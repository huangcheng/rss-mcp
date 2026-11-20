from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from rss_mcp.config import Settings
from rss_mcp.dialect import PostgresDialect, SqliteDialect, dialect_for


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite database with WAL and foreign keys enabled."""
    return SqliteDialect().connect(db_path)


def connect_db(settings: Settings, *, autocommit: bool = False) -> Any:
    """Open the configured database: RSS_MCP_DB_URL wins over RSS_MCP_DB.

    `autocommit=True` is for long-lived server connections. psycopg opens a
    transaction on the first SELECT and keeps it open until commit/rollback;
    such a connection then holds ACCESS SHARE on every table it touched, which
    blocks the next ALTER (2026-11-21: a read left open blocked the hourly
    prune's migration for 2h50m and hung /healthz behind it).
    """
    url = settings.db_url
    if url:
        if url.startswith(("postgresql://", "postgres://")):
            return PostgresDialect().connect(url, autocommit=autocommit)
        if url.startswith("sqlite:///"):
            return connect(Path(url[len("sqlite:///"):]).expanduser())
        raise ValueError(f"unsupported RSS_MCP_DB_URL scheme: {url.partition('://')[0]}")
    return connect(settings.db_path)


def init_db(conn: Any) -> None:
    """Create the schema and apply pending migrations for the connection's engine."""
    dialect_for(conn).init_schema(conn)