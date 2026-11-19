from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rss_mcp.db import connect, init_db
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "rss.db")
    init_db(connection)
    return connection


@pytest.fixture
def store(conn: sqlite3.Connection) -> Store:
    return Store(conn)


def _seed(store: Store, url: str = "https://example.com/feed.xml") -> int:
    return store.add_subscription(url).id


def test_exclude_keywords_hide_entries_from_listing_and_search(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [
            EntryInput(guid="g1", url="https://example.com/1", title="Real news", summary="worth reading"),
            EntryInput(guid="g2", url="https://example.com/2", title="Sponsored post", summary="buy things"),
            EntryInput(guid="g3", url="https://example.com/3", title="More real news", summary="also good"),
        ],
        exclude_keywords=["sponsored"],
    )
    listed = store.list_entries(subscription_id=sub_id, limit=10)
    assert [entry.guid for entry in listed] == ["g3", "g1"]
    found = store.search_entries("news", subscription_id=sub_id, limit=10)
    assert [entry.guid for entry in found] == ["g1", "g3"]
    found_any = store.search_entries("post", limit=10)
    assert found_any == []


def test_keyword_match_is_case_insensitive_and_spans_title_and_summary(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [
            EntryInput(guid="g1", url="https://example.com/1", title="quiet title", summary="mentions WEBINAR here"),
            EntryInput(guid="g2", url="https://example.com/2", title="Fine entry", summary="fine summary"),
        ],
        exclude_keywords=["webinar"],
    )
    assert [entry.guid for entry in store.list_entries(subscription_id=sub_id, limit=10)] == ["g2"]


def test_include_keywords_require_a_match_and_exclude_wins(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [
            EntryInput(guid="g1", url="https://example.com/1", title="Python release notes", summary="3.14 out"),
            EntryInput(guid="g2", url="https://example.com/2", title="Unrelated post", summary="cat pictures"),
            EntryInput(guid="g3", url="https://example.com/3", title="Python but sponsored", summary="buy our course"),
        ],
        include_keywords=["python"],
        exclude_keywords=["sponsored"],
    )
    assert [entry.guid for entry in store.list_entries(subscription_id=sub_id, limit=10)] == ["g1"]


def test_blank_keywords_are_ignored(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g1", url="https://example.com/1", title="Anything")],
        exclude_keywords=["  ", ""],
    )
    assert len(store.list_entries(subscription_id=sub_id, limit=10)) == 1


def test_filtered_entries_stay_hidden_even_with_explicit_subscription(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g1", url="https://example.com/1", title="Hidden", summary="nope")],
        exclude_keywords=["hidden"],
    )
    assert store.list_entries(subscription_id=sub_id, limit=10) == []
    # Explicit id access still works so known ids keep resolving.
    row = store._conn.execute("SELECT id FROM entries WHERE guid = 'g1'").fetchone()
    assert store.get_entry(row["id"]) is not None


def test_rule_change_reevaluates_cached_entries_both_ways(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g1", url="https://example.com/1", title="Sponsored post", summary="ad")],
    )
    assert len(store.list_entries(subscription_id=sub_id, limit=10)) == 1

    store.update_subscription(sub_id, exclude_keywords=["sponsored"])
    assert store.list_entries(subscription_id=sub_id, limit=10) == []

    store.update_subscription(sub_id, exclude_keywords=[])
    assert len(store.list_entries(subscription_id=sub_id, limit=10)) == 1


def test_later_fetches_apply_current_rules_to_new_entries(store: Store) -> None:
    sub_id = _seed(store)
    sub = store.get_subscription(sub_id)
    assert sub is not None
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g1", url="https://example.com/1", title="Sponsored one", summary="ad")],
        exclude_keywords=sub.exclude_keywords,
    )
    assert len(store.list_entries(subscription_id=sub_id, limit=10)) == 1
    # New rules only affect new/changed rows here; reevaluation covers the rest.
    store.update_subscription(sub_id, exclude_keywords=["sponsored"])
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g2", url="https://example.com/2", title="Sponsored two", summary="ad")],
        exclude_keywords=["sponsored"],
    )
    listed = store.list_entries(subscription_id=sub_id, include_duplicates=True, limit=10)
    assert listed == []


def test_muted_subscription_hidden_from_aggregates_but_not_explicit_queries(store: Store) -> None:
    quiet_id = store.add_subscription("https://example.com/quiet.xml").id
    loud_id = store.add_subscription("https://example.com/loud.xml").id
    store.upsert_entries(
        quiet_id,
        [EntryInput(guid="q1", url="https://example.com/q1", title="Quiet one")],
    )
    store.upsert_entries(
        loud_id,
        [EntryInput(guid="l1", url="https://example.com/l1", title="Loud one")],
    )

    store.update_subscription(quiet_id, muted=True)

    aggregate = store.list_entries(limit=10)
    assert [entry.guid for entry in aggregate] == ["l1"]
    explicit = store.list_entries(subscription_id=quiet_id, limit=10)
    assert [entry.guid for entry in explicit] == ["q1"]
    searched = store.search_entries("one", limit=10)
    assert [entry.guid for entry in searched] == ["l1"]
    searched_explicit = store.search_entries("one", subscription_id=quiet_id, limit=10)
    assert [entry.guid for entry in searched_explicit] == ["q1"]

    subs = {sub.id: sub for sub in store.list_subscriptions()}
    assert subs[quiet_id].muted is True
    assert subs[quiet_id].unread_count == 0
    assert subs[loud_id].unread_count == 1


def test_unmute_restores_unread_counts(store: Store) -> None:
    sub_id = _seed(store)
    store.upsert_entries(
        sub_id,
        [EntryInput(guid="g1", url="https://example.com/1", title="Entry")],
    )
    store.update_subscription(sub_id, muted=True)
    assert store.get_subscription(sub_id).unread_count == 0
    store.update_subscription(sub_id, muted=False)
    assert store.get_subscription(sub_id).unread_count == 1


def test_add_subscription_persists_mute_and_rules(store: Store) -> None:
    sub = store.add_subscription(
        "https://example.com/feed.xml",
        muted=True,
        include_keywords=[" keep ", "", "python"],
        exclude_keywords=["sponsor"],
    )
    fetched = store.get_subscription(sub.id)
    assert fetched.muted is True
    assert fetched.include_keywords == ["keep", "python"]
    assert fetched.exclude_keywords == ["sponsor"]

    store.update_subscription(sub.id, include_keywords=[])
    fetched = store.get_subscription(sub.id)
    assert fetched.include_keywords == []
    assert fetched.exclude_keywords == ["sponsor"]
