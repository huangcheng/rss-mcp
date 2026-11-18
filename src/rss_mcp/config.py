from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_USER_AGENT = "rss-mcp/0.1 (+https://github.com/huangcheng/rss-mcp)"


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional_int_env(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class Settings:
    db_path: Path
    user_agent: str
    default_limit: int = 20
    max_limit: int = 100
    fetch_entry_cap: int = 50
    auth_token: str | None = None
    auth_tokens: tuple[str, ...] = ()
    fetch_concurrency: int = 8
    refresh_interval: int | None = None
    disable_after_failures: int = 5
    retention_days: int | None = None
    rate_limit_per_minute: int = 120

    def tokens(self) -> list[str]:
        combined = list(self.auth_tokens)
        if self.auth_token:
            combined.append(self.auth_token)
        seen: set[str] = set()
        unique: list[str] = []
        for token in combined:
            if token and token not in seen:
                seen.add(token)
                unique.append(token)
        return unique


def load_settings() -> Settings:
    raw_db = os.environ.get("RSS_MCP_DB")
    if raw_db and raw_db.strip():
        db_path = Path(raw_db.strip()).expanduser()
    else:
        db_path = Path.home() / ".local" / "share" / "rss-mcp" / "rss.db"
    user_agent = os.environ.get("RSS_MCP_USER_AGENT") or _DEFAULT_USER_AGENT
    auth_token = (os.environ.get("RSS_MCP_AUTH_TOKEN") or "").strip() or None
    auth_tokens = tuple(
        part.strip()
        for part in (os.environ.get("RSS_MCP_AUTH_TOKENS") or "").split(",")
        if part.strip()
    )
    return Settings(
        db_path=db_path,
        user_agent=user_agent,
        auth_token=auth_token,
        auth_tokens=auth_tokens,
        fetch_concurrency=max(1, _int_env("RSS_MCP_FETCH_CONCURRENCY", 8)),
        refresh_interval=_optional_int_env("RSS_MCP_REFRESH_INTERVAL"),
        disable_after_failures=max(0, _int_env("RSS_MCP_DISABLE_AFTER_FAILURES", 5)),
        retention_days=_optional_int_env("RSS_MCP_RETENTION_DAYS"),
        rate_limit_per_minute=max(0, _int_env("RSS_MCP_RATE_LIMIT_PER_MINUTE", 120)),
    )