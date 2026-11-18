import sqlite3
from pathlib import Path

import pytest

from rss_mcp.db import connect, init_db


def test_init_db_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "rss.db"
    conn = connect(db_path)
    assert db_path.is_file()
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    init_db(conn)
    init_db(conn)

    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert {"subscriptions", "entries", "entry_state", "entries_fts"} <= names

    def columns(table: str) -> list[str]:
        return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]

    assert columns("subscriptions") == [
        "id",
        "url",
        "title",
        "tags",
        "etag",
        "last_modified",
        "last_fetched_at",
        "last_error",
        "created_at",
        "user_agent",
        "headers",
        "failure_count",
        "disabled",
    ]
    assert columns("entries") == [
        "id",
        "subscription_id",
        "guid",
        "url",
        "title",
        "author",
        "published_at",
        "summary",
        "content",
        "fetched_at",
        "enclosures",
    ]
    assert columns("entry_state") == ["entry_id", "read_at", "starred"]

    fts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'entries_fts'"
    ).fetchone()["sql"]
    normalized = fts_sql.replace('"', "'")
    assert "content='entries'" in normalized
    assert "content_rowid='id'" in normalized

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO subscriptions (url) VALUES (?)",
            ("https://example.com/missing-created.xml",),
        )
    conn.execute(
        "INSERT INTO subscriptions (url, created_at) VALUES (?, ?)",
        ("https://example.com/feed.xml", "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO subscriptions (url, created_at) VALUES (?, ?)",
            ("https://example.com/feed.xml", "2026-01-02T00:00:00Z"),
        )

    sub_id = conn.execute("SELECT id FROM subscriptions").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entries (subscription_id, guid) VALUES (?, ?)",
            (sub_id, "guid-missing-fetched"),
        )
    conn.execute(
        """INSERT INTO entries
           (subscription_id, guid, title, summary, content, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            sub_id,
            "guid-1",
            "Alpha title",
            "Alpha summary",
            "Alpha content",
            "2026-01-01T00:00:00Z",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO entries (subscription_id, guid, fetched_at)
               VALUES (?, ?, ?)""",
            (sub_id, "guid-1", "2026-01-01T00:00:00Z"),
        )

    entry_id = conn.execute(
        "SELECT id FROM entries WHERE guid = ?",
        ("guid-1",),
    ).fetchone()["id"]
    conn.execute("INSERT INTO entry_state (entry_id) VALUES (?)", (entry_id,))
    state = conn.execute(
        "SELECT read_at, starred FROM entry_state WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    assert state["read_at"] is None
    assert state["starred"] == 0

    assert _fts_ids(conn, "Alpha") == [entry_id]

    conn.execute(
        "UPDATE entries SET title = ?, summary = ?, content = ? WHERE id = ?",
        ("Beta title", "Beta summary", "Beta content", entry_id),
    )
    assert _fts_ids(conn, "Alpha") == []
    assert _fts_ids(conn, "Beta") == [entry_id]

    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    assert _fts_ids(conn, "Beta") == []
    assert conn.execute("SELECT COUNT(*) AS n FROM entry_state").fetchone()["n"] == 0


def test_init_db_migrates_0_1_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            tags TEXT,
            etag TEXT,
            last_modified TEXT,
            last_fetched_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            guid TEXT NOT NULL,
            url TEXT,
            title TEXT,
            author TEXT,
            published_at TEXT,
            summary TEXT,
            content TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE (subscription_id, guid)
        );
        INSERT INTO subscriptions (url, created_at) VALUES ('https://example.com/old.xml', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    migrated = connect(db_path)
    init_db(migrated)
    sub_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(subscriptions)")}
    entry_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(entries)")}
    assert {"user_agent", "headers", "failure_count", "disabled"} <= sub_columns
    assert "enclosures" in entry_columns
    row = migrated.execute("SELECT failure_count, disabled FROM subscriptions").fetchone()
    assert row["failure_count"] == 0
    assert row["disabled"] == 0
    assert migrated.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    migrated.close()


def test_foreign_keys_cascade_delete_subscription(tmp_path: Path) -> None:
    conn = connect(tmp_path / "rss.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO subscriptions (url, title, created_at) VALUES (?, ?, ?)",
        ("https://example.com/a.xml", "A", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO subscriptions (url, title, created_at) VALUES (?, ?, ?)",
        ("https://example.com/b.xml", "B", "2026-01-01T00:00:00Z"),
    )
    a_id = conn.execute(
        "SELECT id FROM subscriptions WHERE url = ?",
        ("https://example.com/a.xml",),
    ).fetchone()["id"]
    b_id = conn.execute(
        "SELECT id FROM subscriptions WHERE url = ?",
        ("https://example.com/b.xml",),
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, title, fetched_at)
           VALUES (?, ?, ?, ?)""",
        (a_id, "a-1", "Gone", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO entries (subscription_id, guid, title, fetched_at)
           VALUES (?, ?, ?, ?)""",
        (b_id, "b-1", "Stays", "2026-01-01T00:00:00Z"),
    )
    a_entry = conn.execute(
        "SELECT id FROM entries WHERE guid = ?",
        ("a-1",),
    ).fetchone()["id"]
    b_entry = conn.execute(
        "SELECT id FROM entries WHERE guid = ?",
        ("b-1",),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO entry_state (entry_id, read_at, starred) VALUES (?, ?, ?)",
        (a_entry, "2026-01-02T00:00:00Z", 1),
    )
    conn.execute("INSERT INTO entry_state (entry_id) VALUES (?)", (b_entry,))
    conn.commit()

    conn.execute("DELETE FROM subscriptions WHERE id = ?", (a_id,))
    conn.commit()

    remaining_entries = conn.execute(
        "SELECT subscription_id, guid FROM entries ORDER BY id"
    ).fetchall()
    assert [(row["subscription_id"], row["guid"]) for row in remaining_entries] == [
        (b_id, "b-1")
    ]
    remaining_state = [
        row["entry_id"]
        for row in conn.execute("SELECT entry_id FROM entry_state ORDER BY entry_id")
    ]
    assert remaining_state == [b_entry]
    assert _fts_ids(conn, "Gone") == []
    assert _fts_ids(conn, "Stays") == [b_entry]


def _fts_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    rows = conn.execute(
        "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?",
        (query,),
    ).fetchall()
    return [row["rowid"] for row in rows]
