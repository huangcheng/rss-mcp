from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerSettings:
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "rss-mcp-refresh"
    mcp_url: str = "http://127.0.0.1:8000/mcp"
    mcp_token: str | None = None
    refresh_interval_minutes: int = 30


def load_settings() -> SchedulerSettings:
    raw_interval = (os.environ.get("RSS_MCP_REFRESH_INTERVAL") or "").strip()
    try:
        interval = int(raw_interval) if raw_interval else 30
    except ValueError:
        interval = 30
    return SchedulerSettings(
        temporal_address=os.environ.get("TEMPORAL_ADDRESS") or "localhost:7233",
        temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE") or "default",
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE") or "rss-mcp-refresh",
        mcp_url=os.environ.get("RSS_MCP_URL") or "http://127.0.0.1:8000/mcp",
        mcp_token=(os.environ.get("RSS_MCP_TOKEN") or "").strip() or None,
        refresh_interval_minutes=max(1, interval),
    )