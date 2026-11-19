"""PostgreSQL integration tests.

Uses RSS_MCP_TEST_PG_URL when set (e.g. a CI service container); otherwise
starts a throwaway postgres:16-alpine container via Docker. Skips when
neither is available or psycopg is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from rss_mcp.config import Settings
from rss_mcp.db import connect_db, init_db
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


def _settings(pg_url: str) -> Settings:
    return Settings(db_path=Path("/tmp/unused.db"), user_agent="rss-mcp/test", db_url=pg_url)


@pytest.fixture()
def pg_store(pg_url):
    conn = connect_db(_settings(pg_url))
    init_db(conn)
    conn.execute("TRUNCATE subscriptions, entries, entry_state RESTART IDENTITY CASCADE")
    conn.commit()
    yield Store(conn)
    conn.close()


def test_schema_init_is_idempotent(pg_url):
    conn = connect_db(_settings(pg_url))
    init_db(conn)
    init_db(conn)
    columns = {
        row["column_name"]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'entries'"
        ).fetchall()
    }
    assert {"enclosures", "search_vector"} <= columns
    conn.close()


def test_subscription_crud_tags_and_filter(pg_store):
    sub = pg_store.add_subscription(
        "https://example.com/a.xml",
        title="A",
        tags=["News"],
        user_agent="custom-agent",
        headers={"Accept": "application/rss+xml"},
    )
    pg_store.add_subscription("https://example.com/b.xml", title="B", tags=["Tech"])

    assert sub.user_agent == "custom-agent"
    assert sub.headers == {"Accept": "application/rss+xml"}
    assert [s.url for s in pg_store.list_subscriptions(tag="News")] == ["https://example.com/a.xml"]
    assert len(pg_store.list_subscriptions()) == 2

    # Re-adding the same URL returns the existing row without duplicating.
    again = pg_store.add_subscription("https://example.com/a.xml")
    assert again.id == sub.id

    assert pg_store.update_subscription(sub.id, tags=["Tech"], disabled=True) is True
    refreshed = pg_store.get_subscription(sub.id)
    assert refreshed.tags == ["Tech"]
    assert refreshed.disabled is True

    assert pg_store.remove_subscription(sub.id) is True
    assert pg_store.get_subscription(sub.id) is None


def test_upsert_and_search_with_snippet(pg_store):
    sub = pg_store.add_subscription("https://example.com/search.xml")
    written = pg_store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="s1", title="Zephyr report", content="The zephyr winds blow strongly today."),
            EntryInput(guid="s2", title="Unrelated", content="Nothing to see here."),
        ],
    )
    assert written == 2

    # Same guid, changed title: exactly one row updated, no duplicate.
    assert pg_store.upsert_entries(sub.id, [EntryInput(guid="s1", title="Zephyr report v2")]) == 1
    assert len(pg_store.list_entries(limit=10)) == 2

    hits = pg_store.search_entries("zephyr")
    assert len(hits) == 1
    assert hits[0].title == "Zephyr report v2"
    assert hits[0].snippet is not None
    assert "[" in hits[0].snippet and "zephyr" in hits[0].snippet.lower()

    assert pg_store.search_entries("nonexistentword") == []
    assert pg_store.search_entries("   ") == []


def test_mark_prune_and_cascade(pg_store):
    sub = pg_store.add_subscription("https://example.com/lifecycle.xml")
    pg_store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="old", title="Old", published_at="2020-01-01T00:00:00Z"),
            EntryInput(guid="new", title="New", published_at="2025-11-01T00:00:00Z"),
        ],
    )
    entries = {e.guid: e for e in pg_store.list_entries(limit=10)}
    assert pg_store.mark_entries([entries["old"].id], True) == 1
    assert pg_store.get_entry(entries["old"].id).read_at is not None

    pg_store.mark_entries([entries["old"].id], True)  # idempotent
    assert pg_store.mark_subscription_entries(sub.id, True) == 1  # only "new" changes

    conn = pg_store._conn
    conn.execute(
        "INSERT INTO entry_state (entry_id, starred) VALUES (?, 1) "
        "ON CONFLICT (entry_id) DO UPDATE SET starred = 1",
        (entries["old"].id,),
    )
    conn.commit()
    assert pg_store.prune_entries("2026-01-01T00:00:00Z") == 1  # "new" pruned, starred "old" kept
    assert {e.guid for e in pg_store.list_entries(limit=10)} == {"old"}

    assert pg_store.remove_subscription(sub.id) is True
    assert pg_store.list_entries(limit=10) == []


def test_healthz_reports_ok_on_postgres(pg_url):
    import httpx

    from rss_mcp.server import build_http_app, create_server

    settings = _settings(pg_url)
    conn = connect_db(settings)
    init_db(conn)
    conn.execute("TRUNCATE subscriptions, entries, entry_state RESTART IDENTITY CASCADE")
    conn.commit()
    Store(conn).add_subscription("https://example.com/health.xml")
    conn.close()

    app = build_http_app(create_server(settings), settings)

    async def probe():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/healthz")

    import asyncio

    response = asyncio.run(probe())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["subscriptions"] == 1


def test_null_published_at_orders_last(pg_store):
    sub = pg_store.add_subscription("https://example.com/order.xml")
    pg_store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="undated", title="Undated zephyr", content="zephyr"),
            EntryInput(guid="dated", title="Dated zephyr", content="zephyr", published_at="2026-01-01T00:00:00Z"),
        ],
    )
    listed = pg_store.list_entries(limit=10)
    assert [e.guid for e in listed] == ["dated", "undated"]
    hits = pg_store.search_entries("zephyr")
    assert [e.guid for e in hits] == ["dated", "undated"]


def test_upsert_null_to_value_updates(pg_store):
    # Pins NULL-safe inequality: a previously-NULL field gaining a value must
    # update even when every non-NULL field is unchanged.
    sub = pg_store.add_subscription("https://example.com/nullfill.xml")
    assert pg_store.upsert_entries(sub.id, [EntryInput(guid="g", title="Same")]) == 1
    assert pg_store.upsert_entries(sub.id, [EntryInput(guid="g", title="Same", summary="now set")]) == 1
    entry = pg_store.list_entries(limit=10)[0]
    assert entry.summary == "now set"


def test_search_with_filters(pg_store):
    sub_a = pg_store.add_subscription("https://example.com/fa.xml")
    sub_b = pg_store.add_subscription("https://example.com/fb.xml")
    pg_store.upsert_entries(sub_a.id, [EntryInput(guid="a1", title="Zephyr alpha", content="zephyr")])
    pg_store.upsert_entries(sub_b.id, [EntryInput(guid="b1", title="Zephyr beta", content="zephyr")])

    scoped = pg_store.search_entries("zephyr", subscription_id=sub_a.id)
    assert [e.guid for e in scoped] == ["a1"]

    pg_store.mark_subscription_entries(sub_a.id, True)
    unread = pg_store.search_entries("zephyr", unread_only=True)
    assert [e.guid for e in unread] == ["b1"]
    both = pg_store.search_entries("zephyr", subscription_id=sub_a.id, unread_only=True)
    assert both == []


def test_diacritic_folding_matches_sqlite(pg_store):
    # SQLite's unicode61 tokenizer folds accents; PG must too (unaccent).
    sub = pg_store.add_subscription("https://example.com/cafe.xml")
    pg_store.upsert_entries(sub.id, [EntryInput(guid="c1", title="café culture", content="about café")])
    hits = pg_store.search_entries("cafe")
    assert len(hits) == 1
    assert hits[0].guid == "c1"


def test_cross_feed_dedup_on_postgres(pg_store):
    a = pg_store.add_subscription("https://a.example/feed.xml")
    b = pg_store.add_subscription("https://b.example/feed.xml")
    c = pg_store.add_subscription("https://c.example/feed.xml")
    pg_store.upsert_entries(
        a.id,
        [EntryInput(guid="a1", url="https://news.example/story?utm_source=rss", title="Story",
                    published_at="2026-11-01T00:00:00Z")],
    )
    pg_store.upsert_entries(
        b.id,
        [EntryInput(guid="b1", url="https://news.example/story", title="Story",
                    published_at="2026-11-01T00:00:00Z")],
    )
    pg_store.upsert_entries(
        c.id,
        [EntryInput(guid="c1", url="https://news.example/story", title="Story",
                    published_at="2026-11-01T00:00:00Z")],
    )
    visible = pg_store.list_entries(limit=10)
    assert len(visible) == 1
    assert pg_store.get_subscription(b.id).unread_count == 0
    everything = pg_store.list_entries(limit=10, include_duplicates=True)
    assert len(everything) == 3
    assert len(pg_store.search_entries("Story")) == 1

    # Deleting the keeper must re-elect, not reveal all copies.
    pg_store.remove_subscription(a.id)
    revealed = pg_store.list_entries(limit=10)
    assert len(revealed) == 1
    assert revealed[0].subscription_id in (b.id, c.id)
    assert revealed[0].duplicate_of is None

def test_filters_and_mute_mirror_sqlite_behavior(pg_store):
    quiet = pg_store.add_subscription("https://q.example/f", muted=True)
    noisy = pg_store.add_subscription("https://n.example/f", exclude_keywords=["sponsored"])
    pg_store.upsert_entries(
        quiet.id,
        [EntryInput(guid="q1", url="https://x.example/q", title="Quiet", summary="hidden in aggregates")],
    )
    pg_store.upsert_entries(
        noisy.id,
        [
            EntryInput(guid="n1", url="https://x.example/n1", title="Real", summary="keep"),
            EntryInput(guid="n2", url="https://x.example/n2", title="Sponsored", summary="drop"),
        ],
        exclude_keywords=["sponsored"],
    )

    aggregate = pg_store.list_entries(limit=10)
    assert [entry.guid for entry in aggregate] == ["n1"]
    explicit = pg_store.list_entries(subscription_id=quiet.id, limit=10)
    assert [entry.guid for entry in explicit] == ["q1"]
    searched = pg_store.search_entries("quiet OR sponsored", limit=10)
    assert searched == []
    subs = {row.id: row for row in pg_store.list_subscriptions()}
    assert subs[quiet.id].unread_count == 0
    assert subs[noisy.id].unread_count == 1

    pg_store.update_subscription(noisy.id, exclude_keywords=[])
    # The muted feed still stays out of aggregates; only the two noisy entries return.
    assert len(pg_store.list_entries(limit=10)) == 2


def test_sweeps_and_starring_mirror_sqlite_behavior(pg_store):
    sub = pg_store.add_subscription("https://s.example/f", tags=["daily"])
    pg_store.upsert_entries(
        sub.id,
        [EntryInput(guid=f"g{i}", title=f"Entry {i}") for i in range(3)],
    )
    entries = {e.guid: e for e in pg_store.list_entries(subscription_id=sub.id, limit=10)}
    pg_store.set_starred([entries["g2"].id], True)

    assert pg_store.mark_entries_bulk(read=True) == 2
    assert pg_store.get_subscription(sub.id).unread_count == 1
    assert pg_store.get_entry(entries["g2"].id).starred is True

    # Unread sweep flips the two read entries; the starred one was never read.
    assert pg_store.mark_entries_bulk(tag="daily", read=False) == 2
    pg_store.set_starred([entries["g2"].id], False)
    assert pg_store.mark_entries_bulk(tag="daily", read=False) == 0
    assert pg_store.get_subscription(sub.id).unread_count == 3


def test_fetch_interval_roundtrip_on_postgres(pg_store):
    sub = pg_store.add_subscription("https://pg-interval.example/f", fetch_interval=7200)
    assert pg_store.get_subscription(sub.id).fetch_interval == 7200
    pg_store.update_subscription(sub.id, fetch_interval=0)
    assert pg_store.get_subscription(sub.id).fetch_interval is None


def test_feed_hints_roundtrip_on_postgres(pg_store):
    sub = pg_store.add_subscription("https://pg-hints.example/f")
    pg_store.update_feed_hints(sub.id, ttl=45, skip_hours=[2, 3], skip_days=["Sunday"])
    stored = pg_store.get_subscription(sub.id)
    assert stored.ttl == 45
    assert stored.skip_hours == [2, 3]
    assert stored.skip_days == ["Sunday"]
    pg_store.update_feed_hints(sub.id, ttl=None, skip_hours=[], skip_days=[])
    stored = pg_store.get_subscription(sub.id)
    assert stored.ttl is None
    assert stored.skip_hours == []
    assert stored.skip_days == []


def test_pg_migration_restores_feed_hints(pg_url):
    settings = _settings(pg_url)
    conn = connect_db(settings)
    init_db(conn)
    for column in ("ttl", "skip_hours", "skip_days"):
        conn.execute(f"ALTER TABLE subscriptions DROP COLUMN IF EXISTS {column}")
    conn.commit()
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://pg-hints-migrate.example/f")
    store.update_feed_hints(sub.id, ttl=30, skip_hours=[1], skip_days=["Tuesday"])
    stored = store.get_subscription(sub.id)
    assert stored.ttl == 30
    assert stored.skip_hours == [1]
    assert stored.skip_days == ["Tuesday"]
    conn.close()


def test_pg_migration_restores_fetch_interval(pg_url):
    # Simulate a pre-0.3.2 database: drop the column, re-init must restore it.
    settings = _settings(pg_url)
    conn = connect_db(settings)
    init_db(conn)
    conn.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS fetch_interval")
    conn.commit()
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://pg-migrate.example/f", fetch_interval=3600)
    assert store.get_subscription(sub.id).fetch_interval == 3600
    conn.close()


def test_filter_parity_on_postgres(pg_store):
    tech = pg_store.add_subscription("https://tech.example/f", tags=["tech"])
    news = pg_store.add_subscription("https://news.example/f", tags=["news"])
    pg_store.upsert_entries(
        tech.id,
        [
            EntryInput(guid="t-old", title="Zephyr old", content="zephyr", published_at="2026-01-01T00:00:00Z"),
            EntryInput(guid="t-new", title="Zephyr new", content="zephyr", published_at="2026-11-01T00:00:00Z"),
        ],
    )
    pg_store.upsert_entries(
        news.id,
        [EntryInput(guid="n1", title="Zephyr news", content="zephyr", published_at="2026-11-01T00:00:00Z")],
    )
    assert {e.guid for e in pg_store.list_entries(tag="tech")} == {"t-old", "t-new"}
    assert {e.guid for e in pg_store.search_entries("zephyr", tag="news")} == {"n1"}
    assert {e.guid for e in pg_store.search_entries("zephyr", since="2026-06-01T00:00:00Z")} == {"t-new", "n1"}

    new_id = next(e.id for e in pg_store.list_entries(limit=10) if e.guid == "t-new")
    pg_store.set_starred([new_id], True)
    assert {e.guid for e in pg_store.list_entries(starred=True)} == {"t-new"}
    assert "t-new" not in {e.guid for e in pg_store.list_entries(starred=False)}

    assert pg_store.list_subscriptions(failing_only=True) == []
    pg_store.record_fetch_failure(news.id, "HTTP 403")
    failing = pg_store.list_subscriptions(failing_only=True)
    assert [s.id for s in failing] == [news.id]
