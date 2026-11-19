from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rss_mcp.db import connect, init_db
from rss_mcp.models import Entry, EntryInput
from rss_mcp.store import Store


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "rss.db")
    init_db(connection)
    return connection


@pytest.fixture
def store(conn: sqlite3.Connection) -> Store:
    return Store(conn)


def test_upsert_entries_inserts_then_updates_same_guid_without_duplicating(
    store: Store,
) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")

    assert store.upsert_entries(sub.id, []) == 0

    inserted = store.upsert_entries(
        sub.id,
        [
            EntryInput(
                guid="g1",
                url="https://example.com/1",
                title="First",
                author="Ann",
                published_at="2026-01-01T00:00:00Z",
                summary="one",
                content="body-one",
            ),
            EntryInput(guid="g2", title="Second"),
            EntryInput(
                guid="g1",
                url="https://example.com/1",
                title="First revised",
                author="Ann",
                published_at="2026-01-01T00:00:00Z",
                summary="one",
                content="body-one",
            ),
        ],
    )
    assert inserted == 3

    listed = store.list_entries(subscription_id=sub.id, limit=10)
    assert len(listed) == 2
    by_guid = {entry.guid: entry for entry in listed}
    revised = by_guid["g1"]
    assert isinstance(revised, Entry)
    assert revised.subscription_id == sub.id
    assert revised.url == "https://example.com/1"
    assert revised.title == "First revised"
    assert revised.author == "Ann"
    assert revised.published_at == "2026-01-01T00:00:00Z"
    assert revised.summary == "one"
    assert revised.content == "body-one"
    assert revised.fetched_at.endswith("Z")
    assert revised.read_at is None
    assert revised.starred is False
    assert by_guid["g2"].title == "Second"

    assert (
        store.upsert_entries(
            sub.id,
            [
                EntryInput(
                    guid="g1",
                    url="https://example.com/1",
                    title="First revised",
                    author="Ann",
                    published_at="2026-01-01T00:00:00Z",
                    summary="one",
                    content="body-one",
                )
            ],
        )
        == 0
    )
    assert len(store.list_entries(subscription_id=sub.id, limit=10)) == 2


def test_upsert_entries_builds_guid_from_url_or_title_hash(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")

    stored = store.upsert_entries(
        sub.id,
        [
            EntryInput(url="  https://example.com/a  ", title="From url"),
            EntryInput(guid="   ", url="https://example.com/blank-guid", title="Blank guid"),
            EntryInput(title="Hello"),
            EntryInput(url="   ", title="   "),
            EntryInput(guid="  keep-me  ", url="https://example.com/b", title="Named"),
        ],
    )
    assert stored == 5

    by_title = {
        entry.title: entry.guid
        for entry in store.list_entries(subscription_id=sub.id, limit=10)
    }
    assert by_title["From url"] == "url:https://example.com/a"
    assert by_title["Blank guid"] == "url:https://example.com/blank-guid"
    assert (
        by_title["Hello"]
        == "title:185f8db32271fe25f561a6fc938b2e264306ec304eda518007d1764826381969"
    )
    assert (
        by_title["   "]
        == "title:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert by_title["Named"] == "keep-me"

    assert (
        store.upsert_entries(
            sub.id,
            [EntryInput(url="https://example.com/a", title="From url again")],
        )
        == 1
    )
    again = {
        entry.guid: entry.title
        for entry in store.list_entries(subscription_id=sub.id, limit=10)
    }
    assert again["url:https://example.com/a"] == "From url again"
    assert len(again) == 5


def test_upsert_entries_leaves_content_when_new_content_is_none(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid="g", title="Old", content="cached article")],
    )
    entry_id = store.list_entries(subscription_id=sub.id, limit=1)[0].id

    assert (
        store.upsert_entries(
            sub.id,
            [EntryInput(guid="g", title="New", summary="blurb", content=None)],
        )
        == 1
    )
    updated = store.get_entry(entry_id)
    assert updated is not None
    assert updated.title == "New"
    assert updated.summary == "blurb"
    assert updated.content == "cached article"

    assert (
        store.upsert_entries(
            sub.id,
            [EntryInput(guid="g", title="New", summary="blurb", content="")],
        )
        == 1
    )
    cleared = store.get_entry(entry_id)
    assert cleared is not None
    assert cleared.content == ""


def test_list_entries_orders_by_published_at_then_id_desc(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="old", title="old", published_at="2026-01-01T00:00:00Z"),
            EntryInput(guid="same-low", title="same-low", published_at="2026-01-02T00:00:00Z"),
            EntryInput(guid="latest", title="latest", published_at="2026-01-03T00:00:00Z"),
            EntryInput(guid="same-high", title="same-high", published_at="2026-01-02T00:00:00Z"),
            EntryInput(guid="undated", title="undated", published_at=None),
        ],
    )

    listed = store.list_entries(subscription_id=sub.id, limit=10)

    assert [entry.guid for entry in listed] == [
        "latest",
        "same-high",
        "same-low",
        "old",
        "undated",
    ]


def test_list_entries_filters_subscription_unread_and_since(store: Store) -> None:
    first = store.add_subscription("https://example.com/a.xml")
    second = store.add_subscription("https://example.com/b.xml")
    store.upsert_entries(
        first.id,
        [
            EntryInput(guid="early", title="early", published_at="2026-01-01T00:00:00Z"),
            EntryInput(guid="on-since", title="on-since", published_at="2026-01-02T00:00:00Z"),
            EntryInput(guid="later", title="later", published_at="2026-01-03T00:00:00Z"),
        ],
    )
    store.upsert_entries(
        second.id,
        [EntryInput(guid="other", title="other", published_at="2026-01-04T00:00:00Z")],
    )
    later = next(
        entry
        for entry in store.list_entries(subscription_id=first.id, limit=10)
        if entry.guid == "later"
    )
    assert store.mark_entries([later.id], read=True) == 1

    listed = store.list_entries(
        subscription_id=first.id,
        unread_only=True,
        since="2026-01-02T00:00:00Z",
        limit=10,
    )
    assert [entry.guid for entry in listed] == ["on-since"]

    everything = store.list_entries(limit=10)
    assert {entry.guid for entry in everything} == {"early", "on-since", "later", "other"}
    assert store.get_subscription(first.id) is not None
    assert store.get_subscription(first.id).unread_count == 2
    assert store.get_subscription(second.id).unread_count == 1


def test_list_entries_defaults_to_twenty(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid=f"{index}", title=str(index), published_at=f"{index:04d}")
            for index in range(21)
        ],
    )

    listed = store.list_entries()

    assert len(listed) == 20
    assert listed[0].guid == "20"
    assert listed[-1].guid == "1"


def test_get_entry_returns_none_for_missing_id(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(sub.id, [EntryInput(guid="g", title="Present")])
    present = store.list_entries(limit=1)[0]

    assert store.get_entry(present.id) is not None
    assert store.get_entry(present.id).guid == "g"
    assert store.get_entry(999_999) is None


def test_mark_entries_toggles_read_state_and_unread_count(
    conn: sqlite3.Connection, store: Store
) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="a", title="A"),
            EntryInput(guid="b", title="B"),
        ],
    )
    entries = {entry.guid: entry for entry in store.list_entries(limit=10)}
    conn.execute(
        "INSERT INTO entry_state (entry_id, read_at, starred) VALUES (?, NULL, 1)",
        (entries["a"].id,),
    )
    conn.commit()

    assert store.mark_entries([], read=True) == 0
    assert store.mark_entries([entries["a"].id, entries["a"].id, 999_999], read=True) == 1
    read = store.get_entry(entries["a"].id)
    assert read is not None
    assert read.read_at is not None
    assert read.read_at.endswith("Z")
    assert read.starred is True
    assert store.get_entry(entries["b"].id).read_at is None
    assert store.get_subscription(sub.id).unread_count == 1
    assert store.mark_entries([entries["a"].id], read=True) == 0

    assert store.mark_entries([entries["a"].id, entries["b"].id], read=False) == 1
    unread = store.get_entry(entries["a"].id)
    assert unread is not None
    assert unread.read_at is None
    assert unread.starred is True
    assert store.get_subscription(sub.id).unread_count == 2
    assert store.mark_entries([entries["a"].id], read=False) == 0

    unread_only = store.list_entries(subscription_id=sub.id, unread_only=True, limit=10)
    assert {entry.guid for entry in unread_only} == {"a", "b"}


def test_search_entries_finds_text_and_refreshes_after_content_update(
    store: Store,
) -> None:
    first = store.add_subscription("https://example.com/a.xml")
    second = store.add_subscription("https://example.com/b.xml")
    store.upsert_entries(
        first.id,
        [
            EntryInput(guid="title-hit", title="Rocket launch", summary="pad"),
            EntryInput(guid="summary-hit", title="Notes", summary="rocket fuel"),
            EntryInput(guid="read-hit", title="Rocket diary", content="old"),
        ],
    )
    store.upsert_entries(
        second.id,
        [EntryInput(guid="other", title="Rocket elsewhere")],
    )
    read_hit = next(
        entry
        for entry in store.list_entries(subscription_id=first.id, limit=10)
        if entry.guid == "read-hit"
    )
    store.mark_entries([read_hit.id], read=True)

    title_and_summary = store.search_entries("rocket", subscription_id=first.id, limit=10)
    assert {entry.guid for entry in title_and_summary} == {
        "title-hit",
        "summary-hit",
        "read-hit",
    }

    unread = store.search_entries(
        "rocket",
        subscription_id=first.id,
        unread_only=True,
        limit=10,
    )
    assert {entry.guid for entry in unread} == {"title-hit", "summary-hit"}

    assert [entry.guid for entry in store.search_entries("old")] == ["read-hit"]
    assert store.search_entries("secretphrase") == []
    store.update_entry_content(read_hit.id, "full text with secretphrase")
    store.update_entry_content(999_999, "ignored")
    refreshed = store.search_entries("secretphrase")
    assert [entry.id for entry in refreshed] == [read_hit.id]
    assert refreshed[0].content == "full text with secretphrase"
    assert store.search_entries("old", subscription_id=first.id) == []


def test_search_entries_matches_keywords_in_either_order(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="split", title="Python tools", summary="an rss reader"),
            EntryInput(guid="adjacent", title="Python rss", summary="together"),
            EntryInput(guid="python-only", title="Python tools", summary="unrelated"),
        ],
    )

    assert {entry.guid for entry in store.search_entries("python rss")} == {
        "split",
        "adjacent",
    }
    assert {entry.guid for entry in store.search_entries("rss python")} == {
        "split",
        "adjacent",
    }


def test_search_entries_safe_with_fts_specials(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid="g", title="hello", summary="world")],
    )

    for query in ("hello AND", '""', "*", '"', "NOT", "title:hello", "hello OR", ""):
        found = store.search_entries(query)
        assert isinstance(found, list)

    assert store.search_entries("hello")[0].guid == "g"


def test_list_and_search_entries_clamp_limit_at_100(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(
                guid=f"{index:03d}",
                title=f"needle {index:03d}",
                published_at=f"{index:04d}",
            )
            for index in range(101)
        ],
    )

    listed = store.list_entries(limit=1000)
    assert len(listed) == 100
    assert listed[0].guid == "100"
    assert listed[-1].guid == "001"

    found = store.search_entries("needle", limit=250)
    assert len(found) == 100

    assert store.list_entries(limit=0) == []
    assert store.list_entries(limit=-3) == []
    assert store.search_entries("needle", limit=0) == []
    assert store.search_entries("needle", limit=-1) == []


def test_upsert_null_to_value_updates_with_unchanged_title(store: Store) -> None:
    # Pins NULL-safe inequality on SQLite: a previously-NULL field gaining a
    # value must update even when every non-NULL field is unchanged.
    sub = store.add_subscription("https://example.com/nullfill.xml")
    assert store.upsert_entries(sub.id, [EntryInput(guid="g", title="Same")]) == 1
    assert store.upsert_entries(sub.id, [EntryInput(guid="g", title="Same", summary="now set")]) == 1
    assert store.list_entries(limit=10)[0].summary == "now set"


def test_sweep_marks_all_but_starred(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid=f"g{i}", title=f"Entry {i}") for i in range(3)],
    )
    entries = {entry.guid: entry for entry in store.list_entries(subscription_id=sub.id, limit=10)}
    store.set_starred([entries["g1"].id], True)

    assert store.mark_entries_bulk(read=True) == 2

    kept_starred = store.get_entry(entries["g1"].id)
    assert kept_starred.read_at is None
    assert kept_starred.starred is True
    assert store.get_subscription(sub.id).unread_count == 1


def test_sweep_scopes_by_tag_and_before(store: Store) -> None:
    news = store.add_subscription("https://n.example/f", tags=["news"])
    misc = store.add_subscription("https://m.example/f", tags=["misc"])
    store.upsert_entries(
        news.id,
        [
            EntryInput(guid="n-old", title="Old news", published_at="2026-11-01T00:00:00Z"),
            EntryInput(guid="n-new", title="New news", published_at="2026-11-10T00:00:00Z"),
        ],
    )
    store.upsert_entries(misc.id, [EntryInput(guid="m-1", title="Misc", published_at="2026-11-05T00:00:00Z")])

    assert store.mark_entries_bulk(tag="news", before="2026-11-05T00:00:00Z", read=True) == 1
    unread = {
        (e.subscription_id, e.guid)
        for e in store.list_entries(limit=10, unread_only=True)
    }
    assert unread == {(news.id, "n-new"), (misc.id, "m-1")}


def test_sweep_can_mark_unread(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid=f"g{i}", title=f"Entry {i}") for i in range(3)],
    )
    entries = [entry.id for entry in store.list_entries(subscription_id=sub.id, limit=10)]
    store.mark_entries(entries, True)
    store.set_starred([entries[0]], True)

    # Unread sweeps also spare starred entries.
    assert store.mark_entries_bulk(read=False) == 2
    assert store.get_subscription(sub.id).unread_count == 2
    assert store.get_entry(entries[0]).read_at is not None


def test_set_starred_skips_unknown_and_noop(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(sub.id, [EntryInput(guid="g1", title="One")])
    entry = store.list_entries(subscription_id=sub.id, limit=10)[0]

    assert store.set_starred([entry.id, 999], True) == 1
    assert store.set_starred([entry.id], True) == 0
    assert store.set_starred([entry.id], False) == 1
    assert store.get_entry(entry.id).starred is False
