from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    tags TEXT,
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    user_agent TEXT,
    headers TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
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
    enclosures TEXT,
    UNIQUE (subscription_id, guid)
);

CREATE TABLE IF NOT EXISTS entry_state (
    entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    read_at TEXT,
    starred INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    title,
    summary,
    content,
    content='entries',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS entries_fts_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, title, summary, content)
    VALUES (new.id, new.title, new.summary, new.content);
END;

CREATE TRIGGER IF NOT EXISTS entries_fts_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, title, summary, content)
    VALUES ('delete', old.id, old.title, old.summary, old.content);
END;

CREATE TRIGGER IF NOT EXISTS entries_fts_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, title, summary, content)
    VALUES ('delete', old.id, old.title, old.summary, old.content);
    INSERT INTO entries_fts(rowid, title, summary, content)
    VALUES (new.id, new.title, new.summary, new.content);
END;
"""

# Columns added after 0.1.x; applied to existing databases on startup.
_MIGRATIONS = (
    ("subscriptions", "user_agent", "ALTER TABLE subscriptions ADD COLUMN user_agent TEXT"),
    ("subscriptions", "headers", "ALTER TABLE subscriptions ADD COLUMN headers TEXT"),
    (
        "subscriptions",
        "failure_count",
        "ALTER TABLE subscriptions ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "subscriptions",
        "disabled",
        "ALTER TABLE subscriptions ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
    ),
    ("entries", "enclosures", "ALTER TABLE entries ADD COLUMN enclosures TEXT"),
)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(ddl)
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)