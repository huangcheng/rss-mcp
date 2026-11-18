from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_USER_AGENT = "rss-mcp/0.1 (+https://github.com/...)"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    user_agent: str
    default_limit: int = 20
    max_limit: int = 100
    fetch_entry_cap: int = 50


def load_settings() -> Settings:
    raw_db = os.environ.get("RSS_MCP_DB")
    if raw_db:
        db_path = Path(raw_db).expanduser()
    else:
        db_path = Path.home() / ".local" / "share" / "rss-mcp" / "rss.db"
    user_agent = os.environ.get("RSS_MCP_USER_AGENT") or _DEFAULT_USER_AGENT
    return Settings(db_path=db_path, user_agent=user_agent)
