from __future__ import annotations

from dataclasses import dataclass, field


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
    user_agent: str | None = None
    headers: dict[str, str] | None = None
    failure_count: int = 0
    disabled: bool = False
    muted: bool = False
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    fetch_interval: int | None = None
    ttl: int | None = None
    skip_hours: list[int] = field(default_factory=list)
    skip_days: list[str] = field(default_factory=list)
    last_item_count: int = 0


@dataclass
class EntryInput:
    guid: str | None = None
    url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    content: str | None = None
    enclosures: list[dict] | None = None


@dataclass
class Entry:
    id: int
    subscription_id: int
    guid: str
    url: str | None
    title: str | None
    author: str | None
    published_at: str | None
    summary: str | None
    content: str | None
    fetched_at: str
    read_at: str | None
    starred: bool
    enclosures: list[dict] = field(default_factory=list)
    snippet: str | None = None
    duplicate_of: int | None = None