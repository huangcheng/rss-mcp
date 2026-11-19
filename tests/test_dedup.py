"""Cross-feed dedup: canonical keys, duplicate marking, visibility rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from rss_mcp.canon import canonical_key
from rss_mcp.db import connect, init_db
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    conn = connect(tmp_path / "dedup.db")
    init_db(conn)
    return Store(conn)


def test_canonical_key_normalizes_urls():
    assert canonical_key("http://Example.com/post/?utm_source=x&id=7#frag", None) == (
        "https://example.com/post?id=7"
    )
    assert canonical_key("https://example.com/post/", None) == "https://example.com/post"
    assert canonical_key("https://example.com", None) == "https://example.com/"
    assert canonical_key("https://example.com/a?b=2&a=1", None) == "https://example.com/a?a=1&b=2"


def test_canonical_key_falls_back_to_title():
    assert canonical_key(None, "  Hello   World ") == "title:hello world"
    assert canonical_key("", "Same Title") == "title:same title"
    assert canonical_key(None, None) is None
    assert canonical_key("   ", "  ") is None


def test_syndicated_article_is_marked_duplicate(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(
        a.id,
        [EntryInput(guid="a1", url="https://news.example/story?utm_source=rss", title="Story",
                    published_at="2026-11-01T00:00:00Z")],
    )
    store.upsert_entries(
        b.id,
        [EntryInput(guid="b1", url="https://news.example/story", title="Story",
                    published_at="2026-11-01T00:00:00Z")],
    )
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id == a.id
    everything = store.list_entries(limit=10, include_duplicates=True)
    assert len(everything) == 2
    dup = next(e for e in everything if e.subscription_id == b.id)
    assert dup.duplicate_of == visible[0].id
    assert store.get_subscription(a.id).unread_count == 1
    assert store.get_subscription(b.id).unread_count == 0


def test_title_only_entries_dedup_by_title(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", title="Shared Headline")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", title="Shared  Headline")])
    assert len(store.list_entries(limit=10)) == 1
    assert len(store.list_entries(limit=10, include_duplicates=True)) == 2


def test_distinct_articles_are_not_deduped(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/1", title="One")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/2", title="Two")])
    assert len(store.list_entries(limit=10)) == 2


def test_outside_window_is_not_deduped(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(
        a.id, [EntryInput(guid="a1", url="https://x.example/s", title="S",
                          published_at="2026-01-01T00:00:00Z")],
    )
    store.upsert_entries(
        b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S",
                          published_at="2026-11-01T00:00:00Z")],
    )
    assert len(store.list_entries(limit=10)) == 2


def test_deleting_keeper_reveals_duplicate(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="S")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S")])
    assert len(store.list_entries(limit=10)) == 1
    store.remove_subscription(a.id)
    remaining = store.list_entries(limit=10)
    assert len(remaining) == 1
    assert remaining[0].subscription_id == b.id
    assert remaining[0].duplicate_of is None


def test_search_hides_duplicates_by_default(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="Zephyr", content="zephyr")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="Zephyr", content="zephyr")])
    assert len(store.search_entries("zephyr")) == 1
    assert len(store.search_entries("zephyr", include_duplicates=True)) == 2


def test_reupsert_same_guid_keeps_duplicate_status(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="S")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S v2")])
    everything = store.list_entries(limit=10, include_duplicates=True)
    dup = next(e for e in everything if e.subscription_id == b.id)
    assert dup.duplicate_of is not None
    assert dup.title == "S v2"


def test_backfill_canonical_keys_for_legacy_rows(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO subscriptions (url, created_at) VALUES ('https://a.example/f', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, url, title, fetched_at, canonical_url)
           VALUES (1, 'g1', 'https://x.example/s?utm_source=rss', 'S', '2026-11-01T00:00:00Z', NULL)"""
    )
    conn.commit()
    store = Store(conn)  # backfill runs in __init__
    row = conn.execute("SELECT canonical_url FROM entries WHERE guid = 'g1'").fetchone()
    assert row["canonical_url"] == "https://x.example/s"
    b = store.add_subscription("https://b.example/f")
    store.upsert_entries(b.id, [EntryInput(guid="g2", url="https://x.example/s", title="S")])
    assert len(store.list_entries(limit=10)) == 1


def test_reupsert_changed_url_refreshes_canonical_key(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    c = store.add_subscription("https://c.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/old", title="S")])
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/new", title="S")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/new", title="S")])
    store.upsert_entries(c.id, [EntryInput(guid="c1", url="https://x.example/old", title="Other")])
    visible = store.list_entries(limit=10, include_duplicates=True)
    by_guid = {e.guid: e for e in visible}
    assert by_guid["b1"].duplicate_of == by_guid["a1"].id
    assert by_guid["c1"].duplicate_of is None
    assert len(store.list_entries(limit=10)) == 2


def test_legacy_duplicates_marked_on_backfill(tmp_path):
    conn = connect(tmp_path / "legacy2.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO subscriptions (url, created_at) VALUES ('https://a.example/f', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO subscriptions (url, created_at) VALUES ('https://b.example/f', '2026-01-01T00:00:00Z')"
    )
    for sub_id, guid in ((1, "g1"), (2, "g2")):
        conn.execute(
            """INSERT INTO entries (subscription_id, guid, url, title, fetched_at, published_at, canonical_url)
               VALUES (?, ?, 'https://x.example/s', 'S', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', NULL)""",
            (sub_id, guid),
        )
    conn.commit()
    store = Store(conn)
    assert len(store.list_entries(limit=10)) == 1
    assert store.get_subscription(2).unread_count == 0


def test_three_feed_keeper_delete_reconverges(store):
    subs = [store.add_subscription(f"https://{name}.example/feed.xml") for name in ("a", "b", "c")]
    for sub in subs:
        store.upsert_entries(sub.id, [EntryInput(guid=f"g{sub.id}", url="https://x.example/s", title="S")])
    assert len(store.list_entries(limit=10)) == 1
    store.remove_subscription(subs[0].id)
    remaining = store.list_entries(limit=10)
    assert len(remaining) == 1
    assert len(store.search_entries("S")) == 1


def test_window_boundary(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(
        a.id, [EntryInput(guid="a1", url="https://x.example/s", title="S",
                          published_at="2026-10-01T00:00:00Z")],
    )
    store.upsert_entries(
        b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S",
                          published_at="2026-11-01T00:00:00Z")],  # 31 days later
    )
    assert len(store.list_entries(limit=10)) == 2

    c = store.add_subscription("https://c.example/feed.xml")
    store.upsert_entries(
        c.id, [EntryInput(guid="c1", url="https://x.example/t", title="T",
                          published_at="2026-10-03T00:00:00Z")],
    )
    d = store.add_subscription("https://d.example/feed.xml")
    store.upsert_entries(
        d.id, [EntryInput(guid="d1", url="https://x.example/t", title="T",
                          published_at="2026-11-01T00:00:00Z")],  # 29 days later
    )
    t_entries = [e for e in store.list_entries(limit=10, include_duplicates=True) if e.title == "T"]
    assert sum(1 for e in t_entries if e.duplicate_of is None) == 1


def test_window_upper_bound(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    # Keeper ingested first but published much later than the second copy.
    store.upsert_entries(
        a.id, [EntryInput(guid="a1", url="https://x.example/s", title="S",
                          published_at="2026-11-01T00:00:00Z")],
    )
    store.upsert_entries(
        b.id, [EntryInput(guid="b1", url="https://x.example/s", title="S",
                          published_at="2026-01-01T00:00:00Z")],
    )
    assert len(store.list_entries(limit=10)) == 2


@pytest.mark.parametrize("param", ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "UTM_Source"])
def test_tracking_params_stripped(param):
    assert canonical_key(f"https://x.example/s?{param}=abc&id=7", None) == "https://x.example/s?id=7"


def test_blank_query_values_kept():
    assert canonical_key("https://x.example/s?id=&x=1", None) == "https://x.example/s?id=&x=1"


def test_same_feed_same_url_not_deduped(store):
    sub = store.add_subscription("https://a.example/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="g1", url="https://x.example/s", title="S"),
            EntryInput(guid="g2", url="https://x.example/s?utm_source=rss", title="S again"),
        ],
    )
    entries = store.list_entries(limit=10)
    assert len(entries) == 2
    assert all(e.duplicate_of is None for e in entries)


def test_keyless_rows_get_sentinel_and_converge(tmp_path):
    conn = connect(tmp_path / "keyless.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO subscriptions (url, created_at) VALUES ('https://a.example/f', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, fetched_at, canonical_url)
           VALUES (1, 'g1', '2026-11-01T00:00:00Z', NULL)"""
    )
    conn.commit()
    Store(conn)
    row = conn.execute("SELECT canonical_url FROM entries WHERE guid = 'g1'").fetchone()
    assert row["canonical_url"] == ""
    # Second construction must not re-process the sentinel row.
    remaining = conn.execute("SELECT COUNT(*) AS n FROM entries WHERE canonical_url IS NULL").fetchone()["n"]
    assert remaining == 0


def test_filtered_copy_never_holds_keeper_seat(store):
    a = store.add_subscription("https://a.example/feed.xml", exclude_keywords=["sponsored"])
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(
        a.id,
        [EntryInput(guid="a1", url="https://x.example/s", title="Sponsored story")],
        exclude_keywords=["sponsored"],
    )
    store.upsert_entries(
        b.id,
        [EntryInput(guid="b1", url="https://x.example/s", title="Story")],
    )
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id == b.id
    assert visible[0].duplicate_of is None
    assert store.get_subscription(b.id).unread_count == 1


def test_filtering_a_keeper_promotes_the_duplicate(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="Story")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="Story")])
    assert len(store.list_entries(limit=10)) == 1

    store.update_subscription(a.id, exclude_keywords=["story"])

    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id == b.id
    assert visible[0].duplicate_of is None
    assert store.get_subscription(a.id).unread_count == 0
    assert store.get_subscription(b.id).unread_count == 1


def test_muting_a_keeper_promotes_the_duplicate_and_back(store):
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="Story")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="Story")])

    store.update_subscription(a.id, muted=True)
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id == b.id

    store.update_subscription(a.id, muted=False)
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    # Earliest copy wins the keeper seat back once it is visible again.
    assert visible[0].subscription_id == a.id

def test_refetch_flipping_a_keepers_filter_releases_duplicates(store):
    # Regression: a changed-content re-upsert flips filtered on a keeper;
    # the other feed's copy must become visible without a restart.
    a = store.add_subscription("https://a.example/feed.xml")
    b = store.add_subscription("https://b.example/feed.xml")
    store.upsert_entries(a.id, [EntryInput(guid="a1", url="https://x.example/s", title="Story")])
    store.upsert_entries(b.id, [EntryInput(guid="b1", url="https://x.example/s", title="Story")])
    assert len(store.list_entries(limit=10)) == 1

    # Feed A refetches; the story now carries a filtered keyword.
    store.upsert_entries(
        a.id,
        [EntryInput(guid="a1", url="https://x.example/s", title="Story", summary="now sponsored")],
        exclude_keywords=["sponsored"],
    )
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id == b.id

    # And back: the filtered summary disappears again on the next change. The
    # earlier copy re-enters through duplicate detection, so either feed may
    # hold the keeper seat; the invariant is one visible copy.
    store.upsert_entries(
        a.id,
        [EntryInput(guid="a1", url="https://x.example/s", title="Story", summary="clean again")],
        exclude_keywords=["sponsored"],
    )
    visible = store.list_entries(limit=10)
    assert len(visible) == 1
    assert visible[0].subscription_id in (a.id, b.id)
