from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rss_mcp.db import connect, init_db
from rss_mcp.models import Subscription
from rss_mcp.store import Store


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "rss.db")
    init_db(connection)
    return connection


@pytest.fixture
def store(conn: sqlite3.Connection) -> Store:
    return Store(conn)


def test_add_subscription_strips_url_and_returns_tags(
    conn: sqlite3.Connection, store: Store
) -> None:
    added = store.add_subscription(
        "  https://example.com/feed.xml  ",
        title="Example",
        tags=["news", "tech,daily"],
    )

    assert isinstance(added, Subscription)
    assert added.id > 0
    assert added.url == "https://example.com/feed.xml"
    assert added.title == "Example"
    assert added.tags == ["news", "tech,daily"]
    assert added.etag is None
    assert added.last_modified is None
    assert added.last_fetched_at is None
    assert added.last_error is None
    assert added.created_at.endswith("Z")
    assert added.unread_count == 0

    row = conn.execute(
        "SELECT url, title, tags FROM subscriptions WHERE id = ?",
        (added.id,),
    ).fetchone()
    assert row["url"] == "https://example.com/feed.xml"
    assert row["title"] == "Example"
    assert json.loads(row["tags"]) == ["news", "tech,daily"]

    empty = store.add_subscription("https://example.com/bare.xml")
    assert empty.title is None
    assert empty.tags == []
    assert json.loads(
        conn.execute(
            "SELECT tags FROM subscriptions WHERE id = ?",
            (empty.id,),
        ).fetchone()["tags"]
    ) == []


def test_add_subscription_persists_for_a_new_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "rss.db"
    conn = connect(db_path)
    init_db(conn)
    added = Store(conn).add_subscription(
        "https://example.com/feed.xml",
        title="Example",
        tags=["news"],
    )
    conn.close()

    reopened = connect(db_path)
    loaded = Store(reopened).get_subscription(added.id)
    assert loaded is not None
    assert loaded.url == "https://example.com/feed.xml"
    assert loaded.title == "Example"
    assert loaded.tags == ["news"]
    assert loaded.unread_count == 0
    reopened.close()


def test_add_subscription_duplicate_url_returns_existing(store: Store) -> None:
    first = store.add_subscription(
        "https://example.com/feed.xml",
        title="Original",
        tags=["keep"],
    )
    again = store.add_subscription(
        "  https://example.com/feed.xml  ",
        title="Replacement",
        tags=["other"],
    )

    assert again.id == first.id
    assert again.url == first.url
    assert again.title == "Original"
    assert again.tags == ["keep"]
    assert again.created_at == first.created_at
    assert [item.id for item in store.list_subscriptions()] == [first.id]


def test_remove_subscription_reports_whether_a_row_was_deleted(
    conn: sqlite3.Connection, store: Store
) -> None:
    kept = store.add_subscription("https://example.com/keep.xml", title="Keep")
    gone = store.add_subscription("https://example.com/gone.xml", title="Gone")
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, title, fetched_at)
           VALUES (?, ?, ?, ?)""",
        (gone.id, "gone-1", "Gone item", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    assert store.remove_subscription(gone.id) is True
    assert store.get_subscription(gone.id) is None
    assert store.remove_subscription(gone.id) is False
    assert store.remove_subscription(999_999) is False
    assert [item.id for item in store.list_subscriptions()] == [kept.id]
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 0
    )


def test_list_subscriptions_orders_by_id_with_zero_unread(store: Store) -> None:
    first = store.add_subscription("https://example.com/a.xml", title="A")
    second = store.add_subscription("https://example.com/b.xml", title="B", tags=[])

    listed = store.list_subscriptions()

    assert [item.id for item in listed] == [first.id, second.id]
    assert [item.url for item in listed] == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]
    assert [item.unread_count for item in listed] == [0, 0]
    assert listed[0].title == "A"
    assert listed[1].tags == []


def test_list_and_get_count_entries_without_read_at(
    conn: sqlite3.Connection, store: Store
) -> None:
    sub = store.add_subscription("https://example.com/feed.xml")
    other = store.add_subscription("https://example.com/other.xml")
    for guid, read_at in (
        ("unread-no-state", None),
        ("unread-null", None),
        ("read", "2026-01-02T00:00:00Z"),
    ):
        conn.execute(
            """INSERT INTO entries (subscription_id, guid, fetched_at)
               VALUES (?, ?, ?)""",
            (sub.id, guid, "2026-01-01T00:00:00Z"),
        )
        entry_id = conn.execute(
            "SELECT id FROM entries WHERE guid = ?",
            (guid,),
        ).fetchone()["id"]
        if guid != "unread-no-state":
            conn.execute(
                "INSERT INTO entry_state (entry_id, read_at) VALUES (?, ?)",
                (entry_id, read_at),
            )
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, fetched_at)
           VALUES (?, ?, ?)""",
        (other.id, "other-unread", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    listed = {item.id: item.unread_count for item in store.list_subscriptions()}
    assert listed == {sub.id: 2, other.id: 1}
    loaded = store.get_subscription(sub.id)
    assert loaded is not None
    assert loaded.unread_count == 2
    assert store.get_subscription(999_999) is None


def test_update_fetch_meta_persists_conditional_headers(store: Store) -> None:
    sub = store.add_subscription("https://example.com/feed.xml", title="Example")

    store.update_fetch_meta(
        sub.id,
        etag='"v1"',
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        last_fetched_at="2026-01-03T00:00:00Z",
        last_error=None,
    )
    fetched = store.get_subscription(sub.id)
    assert fetched is not None
    assert fetched.etag == '"v1"'
    assert fetched.last_modified == "Wed, 01 Jan 2026 00:00:00 GMT"
    assert fetched.last_fetched_at == "2026-01-03T00:00:00Z"
    assert fetched.last_error is None
    assert fetched.title == "Example"
    assert fetched.url == "https://example.com/feed.xml"

    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at="2026-01-04T00:00:00Z",
        last_error="timeout",
    )
    cleared = store.get_subscription(sub.id)
    assert cleared is not None
    assert cleared.etag is None
    assert cleared.last_modified is None
    assert cleared.last_fetched_at == "2026-01-04T00:00:00Z"
    assert cleared.last_error == "timeout"

    store.update_fetch_meta(
        999_999,
        etag="nope",
        last_modified="nope",
        last_fetched_at="2026-01-05T00:00:00Z",
        last_error="missing",
    )
    untouched = store.get_subscription(sub.id)
    assert untouched is not None
    assert untouched.last_error == "timeout"
    assert untouched.last_fetched_at == "2026-01-04T00:00:00Z"
