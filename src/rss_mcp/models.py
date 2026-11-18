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


@dataclass
class EntryInput:
    guid: str | None = None
    url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    content: str | None = None


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
