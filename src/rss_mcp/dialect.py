from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rss_mcp.db_sqlite import SCHEMA as _SQLITE_SCHEMA, MIGRATIONS as _SQLITE_MIGRATIONS

_PG_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id SERIAL PRIMARY KEY,
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
    )""",
    """CREATE TABLE IF NOT EXISTS entries (
        id SERIAL PRIMARY KEY,
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
    )""",
    """CREATE TABLE IF NOT EXISTS entry_state (
        entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
        read_at TEXT,
        starred INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS server_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS digest_deliveries (
        entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
        delivered_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS digest_deliveries_delivered_at
        ON digest_deliveries (delivered_at)""",
)

_PG_MIGRATIONS = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_agent TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS headers TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS disabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS enclosures TEXT",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS canonical_url TEXT",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS duplicate_of INTEGER REFERENCES entries(id) ON DELETE SET NULL",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS muted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS include_keywords TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS exclude_keywords TEXT",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS filtered INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS fetch_interval INTEGER",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS ttl INTEGER",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS skip_hours TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS skip_days TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_item_count INTEGER",
)

_CANONICAL_INDEX_DDL = "CREATE INDEX IF NOT EXISTS entries_canonical_idx ON entries(canonical_url)"


def _fts_match_query(query: str) -> str | None:
    terms: list[str] = []
    for raw in query.split():
        token = raw.strip('"')
        if not token:
            continue
        terms.append('"' + token.replace('"', '""') + '"')
    if not terms:
        return None
    return " AND ".join(terms)


_SCHEMA_KEY = "schema"
_MIGRATION_LOCK_TIMEOUT = "5s"


def _fingerprint(*statements: str) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _applied_fingerprint(conn: Any) -> str | None:
    """Read the recorded schema fingerprint; None when unreadable/absent."""
    try:
        row = conn.execute(
            "SELECT value FROM server_meta WHERE key = ?", (_SCHEMA_KEY,)
        ).fetchone()
    except Exception:
        # Fresh database: server_meta itself does not exist yet. On PostgreSQL
        # a failed statement poisons the transaction, so roll back.
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    return row["value"] if row else None


def _record_fingerprint(conn: Any, fingerprint: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO server_meta (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (_SCHEMA_KEY, fingerprint, now),
    )
    conn.commit()


class SqliteDialect:
    name = "sqlite"
    insert_ignore = "OR IGNORE"
    conflict_nothing = ""
    null_safe_neq = "IS NOT"
    search_errors: tuple[type[Exception], ...] = (sqlite3.OperationalError,)

    def connect(self, db_path: Path) -> sqlite3.Connection:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_schema(self, conn: sqlite3.Connection) -> None:
        fingerprint = _fingerprint(
            _SQLITE_SCHEMA, _CANONICAL_INDEX_DDL, *(ddl for _, _, ddl in _SQLITE_MIGRATIONS)
        )
        if _applied_fingerprint(conn) == fingerprint:
            return
        conn.executescript(_SQLITE_SCHEMA)
        for table, column, ddl in _SQLITE_MIGRATIONS:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(ddl)
        conn.execute(_CANONICAL_INDEX_DDL)
        conn.commit()
        _record_fingerprint(conn, fingerprint)

    def tags_exists(self, alias: str) -> str:
        return f"EXISTS (SELECT 1 FROM json_each({alias}.tags) WHERE value = ?)"

    def search_sql(
        self,
        entry_columns: str,
        query: str,
        extra_clauses: list[str],
        extra_params: list[Any],
        limit: int,
    ) -> tuple[str, list[Any]] | None:
        match = _fts_match_query(query)
        if match is None:
            return None
        clauses = ["entries_fts MATCH ?", *extra_clauses]
        params: list[Any] = [match, *extra_params, limit]
        sql = f"""SELECT {entry_columns},
                         snippet(entries_fts, -1, '[', ']', '…', 12) AS snippet
                  FROM entries AS e
                  JOIN entries_fts ON entries_fts.rowid = e.id
                  LEFT JOIN entry_state AS es ON es.entry_id = e.id
                  WHERE {" AND ".join(clauses)}
                  ORDER BY bm25(entries_fts), e.published_at DESC NULLS LAST, e.id DESC
                  LIMIT ?"""
        return sql, params


class _PgConnection:
    """psycopg wrapper: dict rows and ? -> %s placeholder translation."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._ts_config: str | None = None

    def execute(self, sql: str, params: Any = ()) -> Any:
        try:
            return self._conn.execute(sql.replace("?", "%s"), params)
        except Exception:
            # Never leave a half-applied batch inside an implicit transaction.
            from psycopg.pq import TransactionStatus

            if self._conn.info.transaction_status != TransactionStatus.IDLE:
                self._conn.rollback()
            raise

    def executemany(self, sql: str, params_seq: Any) -> None:
        # psycopg3 exposes executemany on cursors only.
        with self._conn.cursor() as cursor:
            cursor.executemany(sql.replace("?", "%s"), params_seq)

    def begin_batch(self) -> None:
        """Open one transaction for a multi-row write on an autocommit conn.

        Without this every row is its own transaction (a review measured 504
        commits for 500 rows). The caller's existing commit() ends the batch,
        and execute() rolls back if any statement inside it fails.
        """
        if getattr(self._conn, "autocommit", False):
            self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def ts_config(self) -> str:
        # Must match the configuration the search_vector column was built with.
        if self._ts_config is None:
            row = self.execute(
                "SELECT 1 FROM pg_ts_config WHERE cfgname = 'rss_unaccent'"
            ).fetchone()
            self._ts_config = "rss_unaccent" if row else "simple"
        return self._ts_config


class PostgresDialect:
    name = "postgres"
    insert_ignore = ""
    conflict_nothing = " ON CONFLICT DO NOTHING"
    null_safe_neq = "IS DISTINCT FROM"
    search_errors: tuple[type[Exception], ...] = ()

    def __init__(self, conn: _PgConnection | None = None) -> None:
        self._conn = conn

    def connect(self, url: str, *, autocommit: bool = False) -> _PgConnection:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL support requires the postgres extra: pip install rss-mcp-server[postgres]"
            ) from exc
        return _PgConnection(
            psycopg.connect(url, row_factory=dict_row, autocommit=autocommit)
        )

    def init_schema(self, conn: _PgConnection) -> None:
        # Never queue behind a reader for hours: anything that needs a lock must
        # fail loudly (2026-11-21: a blocked ALTER hung startup and /healthz).
        conn.execute(f"SET lock_timeout = '{_MIGRATION_LOCK_TIMEOUT}'")
        fingerprint = _fingerprint(*_PG_SCHEMA, *_PG_MIGRATIONS, _CANONICAL_INDEX_DDL)
        if _applied_fingerprint(conn) != fingerprint:
            for statement in (*_PG_SCHEMA, *_PG_MIGRATIONS):
                conn.execute(statement)
            conn.execute(_CANONICAL_INDEX_DDL)
            conn.commit()
            self._relax_item_count(conn)
            _record_fingerprint(conn, fingerprint)
        # Not fingerprinted on purpose: whether unaccent can be installed is an
        # environment fact that may change after the schema was recorded (a DBA
        # adds the extension later). Reconcile every start, cheaply.
        self._ensure_unaccent(conn)
        self._ensure_search_column(conn)

    def _ensure_unaccent(self, conn: _PgConnection) -> None:
        """Fold diacritics like SQLite's unicode61 tokenizer when available.

        Restricted deployments fall back to 'simple'. Runs on every start
        because extension availability is not part of the schema fingerprint.
        """
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
            exists = conn.execute(
                "SELECT 1 FROM pg_ts_config WHERE cfgname = 'rss_unaccent'"
            ).fetchone()
            if not exists:
                conn.execute(
                    "CREATE TEXT SEARCH CONFIGURATION rss_unaccent (COPY = simple)"
                )
                conn.execute(
                    "ALTER TEXT SEARCH CONFIGURATION rss_unaccent "
                    "ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple"
                )
            conn.commit()
        except Exception:
            conn.rollback()

    def _relax_item_count(self, conn: _PgConnection) -> None:
        """Undo 0.5.0's NOT NULL DEFAULT 0 on last_item_count.

        That default made every pre-existing subscription look like a feed
        whose last fetch accepted zero items, i.e. 79 false "empty" feeds on
        the live library. NULL must mean "not recorded yet"; a real zero is
        written by the next parsed fetch.
        """
        row = conn.execute(
            """SELECT is_nullable FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'subscriptions'
                 AND column_name = 'last_item_count'"""
        ).fetchone()
        if row is None or row["is_nullable"] == "YES":
            return
        conn.execute("ALTER TABLE subscriptions ALTER COLUMN last_item_count DROP NOT NULL")
        conn.execute("ALTER TABLE subscriptions ALTER COLUMN last_item_count DROP DEFAULT")
        conn.execute("UPDATE subscriptions SET last_item_count = NULL WHERE last_item_count = 0")
        conn.commit()

    def _ensure_search_column(self, conn: _PgConnection) -> None:
        cfg_row = conn.execute(
            "SELECT 1 FROM pg_ts_config WHERE cfgname = 'rss_unaccent'"
        ).fetchone()
        desired = "rss_unaccent" if cfg_row else "simple"
        expr_row = conn.execute(
            "SELECT pg_get_expr(d.adbin, d.adrelid) AS expr FROM pg_attrdef d "
            "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
            "WHERE a.attrelid = 'entries'::regclass AND a.attname = 'search_vector'"
        ).fetchone()
        if expr_row is None or desired not in (expr_row["expr"] or ""):
            conn.execute("ALTER TABLE entries DROP COLUMN IF EXISTS search_vector")
            conn.execute(
                "ALTER TABLE entries ADD COLUMN search_vector tsvector "
                f"GENERATED ALWAYS AS (to_tsvector('{desired}', "
                "coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(content, ''))) STORED"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS entries_search_idx ON entries USING GIN (search_vector)"
        )
        conn.commit()

    def tags_exists(self, alias: str) -> str:
        return (
            f"EXISTS (SELECT 1 FROM jsonb_array_elements_text({alias}.tags::jsonb) AS t(value)"
            " WHERE t.value = ?)"
        )

    def search_sql(
        self,
        entry_columns: str,
        query: str,
        extra_clauses: list[str],
        extra_params: list[Any],
        limit: int,
    ) -> tuple[str, list[Any]] | None:
        if not query.split():
            return None
        cfg = self._conn.ts_config if self._conn is not None else "simple"
        clauses = [f"e.search_vector @@ plainto_tsquery('{cfg}', ?)", *extra_clauses]
        params: list[Any] = [query, query, *extra_params, query, limit]
        sql = f"""SELECT {entry_columns},
                         ts_headline('{cfg}',
                             coalesce(e.title, '') || ' ' || coalesce(e.summary, '') || ' ' || coalesce(e.content, ''),
                             plainto_tsquery('{cfg}', ?),
                             'StartSel=[, StopSel=], MaxWords=12, MinWords=5') AS snippet
                  FROM entries AS e
                  LEFT JOIN entry_state AS es ON es.entry_id = e.id
                  WHERE {" AND ".join(clauses)}
                  ORDER BY ts_rank(e.search_vector, plainto_tsquery('{cfg}', ?)) DESC,
                           e.published_at DESC NULLS LAST, e.id DESC
                  LIMIT ?"""
        return sql, params


def dialect_for(conn: Any) -> SqliteDialect | PostgresDialect:
    if isinstance(conn, sqlite3.Connection):
        return SqliteDialect()
    return PostgresDialect(conn)