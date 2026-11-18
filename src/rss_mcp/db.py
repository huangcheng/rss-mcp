from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from rss_mcp.config import Settings
from rss_mcp.dialect import PostgresDialect, SqliteDialect, dialect_for


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite database with WAL and foreign keys enabled."""
    return SqliteDialect().connect(db_path)


def connect_db(settings: Settings) -> Any:
    """Open the configured database: RSS_MCP_DB_URL wins over RSS_MCP_DB."""
    url = settings.db_url
    if url:
        if url.startswith(("postgresql://", "postgres://")):
            return PostgresDialect().connect(url)
        if url.startswith("sqlite:///"):
            return connect(Path(url[len("sqlite:///"):]).expanduser())
        raise ValueError(f"unsupported RSS_MCP_DB_URL scheme: {url.partition('://')[0]}")
    return connect(settings.db_path)


def init_db(conn: Any) -> None:
    """Create the schema and apply pending migrations for the connection's engine."""
    dialect_for(conn).init_schema(conn)