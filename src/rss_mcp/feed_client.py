from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import feedparser
import httpx

from rss_mcp.models import EntryInput

MAX_FEED_BYTES = 5 * 1024 * 1024
_READ_CHUNK = 64 * 1024


@dataclass
class FetchResult:
    status: Literal["ok", "not_modified", "error"]
    entries: list[EntryInput]
    etag: str | None
    last_modified: str | None
    feed_title: str | None
    error: str | None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _header(response: httpx.Response, name: str) -> str | None:
    return _text(response.headers.get(name))


def _error(message: str) -> FetchResult:
    return FetchResult(
        status="error",
        entries=[],
        etag=None,
        last_modified=None,
        feed_title=None,
        error=message,
    )


def _published_at(entry: feedparser.FeedParserDict) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        moment = datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _content(entry: feedparser.FeedParserDict) -> str | None:
    blocks = entry.get("content") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = _text(block.get("value"))
        if text is not None:
            return text
    return None


def _entry(entry: feedparser.FeedParserDict) -> EntryInput:
    return EntryInput(
        guid=_text(entry.get("id")),
        url=_text(entry.get("link")),
        title=_text(entry.get("title")),
        author=_text(entry.get("author")),
        published_at=_published_at(entry),
        summary=_text(entry.get("summary")),
        content=_content(entry),
    )


def _cap(entries: list[EntryInput], entry_cap: int) -> list[EntryInput]:
    if entry_cap <= 0:
        return []
    return entries[:entry_cap]


def _from_parsed(
    parsed: feedparser.FeedParserDict,
    *,
    etag: str | None,
    last_modified: str | None,
    entry_cap: int,
) -> FetchResult:
    version = parsed.get("version") or ""
    if not isinstance(version, str):
        version = ""
    raw_entries = list(parsed.entries)
    if not raw_entries and not version.strip():
        exc = parsed.get("bozo_exception")
        detail = str(exc).strip() if exc else "unrecognized feed"
        return _error(f"unable to parse feed: {detail}")
    return FetchResult(
        status="ok",
        entries=_cap([_entry(entry) for entry in raw_entries], entry_cap),
        etag=etag,
        last_modified=last_modified,
        feed_title=_text(parsed.feed.get("title")),
        error=None,
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK):
        total += len(chunk)
        if total > MAX_FEED_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_feed(
    url: str,
    *,
    etag: str | None,
    last_modified: str | None,
    user_agent: str,
    entry_cap: int,
    timeout: float = 30.0,
) -> FetchResult:
    sent_etag = _text(etag)
    sent_modified = _text(last_modified)
    headers: dict[str, str] = {}
    if sent_etag is not None:
        headers["If-None-Match"] = sent_etag
    if sent_modified is not None:
        headers["If-Modified-Since"] = sent_modified
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response_etag = _header(response, "etag")
                response_modified = _header(response, "last-modified")
                if response.status_code == 304:
                    return FetchResult(
                        status="not_modified",
                        entries=[],
                        etag=response_etag or sent_etag,
                        last_modified=response_modified or sent_modified,
                        feed_title=None,
                        error=None,
                    )
                if response.status_code != 200:
                    return _error(f"HTTP {response.status_code}")
                body = await _read_limited(response)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return _error(message)
    if body is None:
        return _error(f"response exceeds {MAX_FEED_BYTES} bytes")
    try:
        parsed = feedparser.parse(body)
    except Exception as exc:
        return _error(f"unable to parse feed: {exc}")
    return _from_parsed(
        parsed,
        etag=response_etag,
        last_modified=response_modified,
        entry_cap=entry_cap,
    )
