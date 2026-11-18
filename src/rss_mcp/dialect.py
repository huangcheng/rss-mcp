from __future__ import annotations

import sqlite3
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
        disabled INTEGER NOT NULL DEFAULT 0
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
        UNIQUE (subscription_id, guid)
    )""",
    """CREATE TABLE IF NOT EXISTS entry_state (
        entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
        read_at TEXT,
        starred INTEGER NOT NULL DEFAULT 0
    )""",
)

_PG_MIGRATIONS = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_agent TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS headers TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS disabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS enclosures TEXT",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS canonical_url TEXT",
    "ALTER TABLE entries ADD COLUMN IF NOT EXISTS duplicate_of INTEGER REFERENCES entries(id) ON DELETE SET NULL",
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
        conn.executescript(_SQLITE_SCHEMA)
        for table, column, ddl in _SQLITE_MIGRATIONS:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(ddl)
        conn.execute(_CANONICAL_INDEX_DDL)
        conn.commit()

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
        return self._conn.execute(sql.replace("?", "%s"), params)

    def executemany(self, sql: str, params_seq: Any) -> None:
        self._conn.executemany(sql.replace("?", "%s"), params_seq)

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

    def connect(self, url: str) -> _PgConnection:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL support requires the postgres extra: pip install rss-mcp-server[postgres]"
            ) from exc
        return _PgConnection(psycopg.connect(url, row_factory=dict_row, autocommit=False))

    def init_schema(self, conn: _PgConnection) -> None:
        for statement in (*_PG_SCHEMA, *_PG_MIGRATIONS):
            conn.execute(statement)
        conn.execute(_CANONICAL_INDEX_DDL)
        conn.commit()
        # Fold diacritics like SQLite's unicode61 tokenizer when the unaccent
        # extension is available; restricted deployments fall back to 'simple'.
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
            conn.execute("CREATE TEXT SEARCH CONFIGURATION rss_unaccent (COPY = simple)")
            conn.execute(
                "ALTER TEXT SEARCH CONFIGURATION rss_unaccent "
                "ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple"
            )
            conn.commit()
        except Exception:
            conn.rollback()
        self._ensure_search_column(conn)

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