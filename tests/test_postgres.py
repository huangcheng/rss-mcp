"""PostgreSQL integration tests.

Uses RSS_MCP_TEST_PG_URL when set (e.g. a CI service container); otherwise
starts a throwaway postgres:16-alpine container via Docker. Skips when
neither is available or psycopg is not installed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from rss_mcp.config import Settings
from rss_mcp.db import connect_db, init_db
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def pg_url():
    env_url = os.environ.get("RSS_MCP_TEST_PG_URL")
    if env_url:
        yield env_url
        return
    if not shutil.which("docker"):
        pytest.skip("docker not available for PostgreSQL tests")
    port = _free_port()
    name = f"rss-mcp-test-pg-{port}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_DB=rss_test",
                "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        pytest.skip(f"docker not usable for PostgreSQL tests: {exc}")
    url = f"postgresql://postgres:test@127.0.0.1:{port}/rss_test"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            break
        except Exception:
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("PostgreSQL container did not become ready")
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


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
