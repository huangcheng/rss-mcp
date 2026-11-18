from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Subscription:
    id: int
    url: str
    title: str | None
    tags: list[str]
    etag: str | None
    last_modified: str | None
    last_fetched_at: str | None
    last_error: str | None
    created_at: str
    unread_count: int = 0
