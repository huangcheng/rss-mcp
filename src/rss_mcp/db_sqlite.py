from __future__ import annotations

SCHEMA = """
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
    disabled INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0,
    include_keywords TEXT,
    exclude_keywords TEXT,
    fetch_interval INTEGER,
    ttl INTEGER,
    skip_hours TEXT,
    skip_days TEXT
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
    canonical_url TEXT,
    duplicate_of INTEGER REFERENCES entries(id) ON DELETE SET NULL,
    filtered INTEGER NOT NULL DEFAULT 0,
    UNIQUE (subscription_id, guid)
);

CREATE TABLE IF NOT EXISTS entry_state (
    entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    read_at TEXT,
    starred INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
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
MIGRATIONS = (
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
    ("entries", "canonical_url", "ALTER TABLE entries ADD COLUMN canonical_url TEXT"),
    (
        "entries",
        "duplicate_of",
        "ALTER TABLE entries ADD COLUMN duplicate_of INTEGER REFERENCES entries(id) ON DELETE SET NULL",
    ),
    (
        "subscriptions",
        "muted",
        "ALTER TABLE subscriptions ADD COLUMN muted INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "subscriptions",
        "include_keywords",
        "ALTER TABLE subscriptions ADD COLUMN include_keywords TEXT",
    ),
    (
        "subscriptions",
        "exclude_keywords",
        "ALTER TABLE subscriptions ADD COLUMN exclude_keywords TEXT",
    ),
    (
        "entries",
        "filtered",
        "ALTER TABLE entries ADD COLUMN filtered INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "subscriptions",
        "fetch_interval",
        "ALTER TABLE subscriptions ADD COLUMN fetch_interval INTEGER",
    ),
    ("subscriptions", "ttl", "ALTER TABLE subscriptions ADD COLUMN ttl INTEGER"),
    ("subscriptions", "skip_hours", "ALTER TABLE subscriptions ADD COLUMN skip_hours TEXT"),
    ("subscriptions", "skip_days", "ALTER TABLE subscriptions ADD COLUMN skip_days TEXT"),
)