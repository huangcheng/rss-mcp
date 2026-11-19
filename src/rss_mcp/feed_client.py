from __future__ import annotations

import asyncio
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin

import feedparser
import httpx

from rss_mcp.models import EntryInput

MAX_FEED_BYTES = 5 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
# ttl is stored in a 32-bit INTEGER column and consumed as ttl*60 seconds;
# keep both inside the bounds enforced for fetch_interval.
_MAX_TTL_MINUTES = 2_147_483_647 // 60
_DOCTYPE = re.compile(rb"<!doctype", re.IGNORECASE)


def _is_http_url(url: str | None) -> bool:
    # URI schemes are case-insensitive (RFC 3986); compare lowered, store as-is.
    return bool(url) and url.lower().startswith(("http://", "https://"))


@dataclass
class FetchResult:
    status: Literal["ok", "not_modified", "error"]
    entries: list[EntryInput]
    etag: str | None
    last_modified: str | None
    feed_title: str | None
    error: str | None
    ttl: int | None = None
    skip_hours: list[int] = field(default_factory=list)
    skip_days: list[str] = field(default_factory=list)


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


def _resolve_url(value: object, base_url: str) -> str | None:
    raw = _text(value)
    if raw is None or not base_url:
        return raw
    return _text(urljoin(base_url, raw))


def _enclosures(entry: feedparser.FeedParserDict, base_url: str) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    candidates = list(entry.get("enclosures") or [])
    candidates.extend(
        link for link in (entry.get("links") or []) if link.get("rel") == "enclosure"
    )
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        url = _resolve_url(raw.get("href") or raw.get("url"), base_url)
        # RSS 2.0: enclosure url must be an http url (https accepted as its
        # modern equivalent); other schemes are not fetchable media.
        if not _is_http_url(url) or url in seen:
            continue
        seen.add(url)
        item: dict = {"url": url}
        mime = _text(raw.get("type"))
        if mime:
            item["type"] = mime
        length = _text(raw.get("length"))
        if length:
            item["length"] = length
        found.append(item)
    return found


def _entry(entry: feedparser.FeedParserDict, base_url: str) -> EntryInput:
    url = _resolve_url(entry.get("link"), base_url)
    if not _is_http_url(url):
        url = None
    if url is None and entry.get("guidislink"):
        # RSS 2.0: guid with isPermaLink=true (the default) is a permalink.
        guid_url = _resolve_url(entry.get("id"), base_url)
        if _is_http_url(guid_url):
            url = guid_url
    return EntryInput(
        guid=_text(entry.get("id")),
        url=url,
        title=_text(entry.get("title")),
        author=_text(entry.get("author")),
        published_at=_published_at(entry),
        summary=_text(entry.get("summary")),
        content=_content(entry),
        enclosures=_enclosures(entry, base_url),
    )


def _feed_hints(body: bytes) -> tuple[int | None, list[int], list[str]]:
    """RSS 2.0 channel hints: ttl (minutes), skipHours (0-23 GMT), skipDays.

    Parsed directly: feedparser flattens skipHours/skipDays sub-elements and
    loses the lists. RSS core elements are unnamespaced; Atom has no hints.
    Bodies with a DTD are skipped: hints never need one, and ET expands
    internal entities (billion-laughs amplification).
    """
    if _DOCTYPE.search(body):
        return None, [], []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, [], []
    channel = root.find("channel")
    if channel is None:
        return None, [], []
    ttl: int | None = None
    raw_ttl = (channel.findtext("ttl") or "").strip()
    if raw_ttl:
        try:
            parsed_ttl = int(raw_ttl)
            ttl = parsed_ttl if 0 < parsed_ttl <= _MAX_TTL_MINUTES else None
        except ValueError:
            ttl = None
    hours: list[int] = []
    for hour_el in channel.findall("skipHours/hour"):
        try:
            hour = int((hour_el.text or "").strip())
        except ValueError:
            continue
        if 0 <= hour <= 23 and hour not in hours:
            hours.append(hour)
    days: list[str] = []
    for day_el in channel.findall("skipDays/day"):
        day = (day_el.text or "").strip().title()
        if day in _WEEKDAYS and day not in days:
            days.append(day)
    return ttl, hours, days


def _cap(entries: list[EntryInput], entry_cap: int) -> list[EntryInput]:
    if entry_cap <= 0:
        return []
    return entries[:entry_cap]


def _from_parsed(
    parsed: feedparser.FeedParserDict,
    *,
    base_url: str,
    etag: str | None,
    last_modified: str | None,
    entry_cap: int,
    hints: tuple[int | None, list[int], list[str]],
) -> FetchResult:
    version = parsed.get("version") or ""
    if not isinstance(version, str):
        version = ""
    raw_entries = list(parsed.entries)
    if not raw_entries and not version.strip():
        exc = parsed.get("bozo_exception")
        detail = str(exc).strip() if exc else "unrecognized feed"
        return _error(f"unable to parse feed: {detail}")
    ttl, skip_hours, skip_days = hints
    return FetchResult(
        status="ok",
        entries=_cap(
            [_entry(entry, base_url) for entry in raw_entries],
            entry_cap,
        ),
        etag=etag,
        last_modified=last_modified,
        feed_title=_text(parsed.feed.get("title")),
        error=None,
        ttl=ttl,
        skip_hours=skip_hours,
        skip_days=skip_days,
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
    headers: dict[str, str] | None = None,
) -> FetchResult:
    sent_etag = _text(etag)
    sent_modified = _text(last_modified)
    request_headers: dict[str, str] = {}
    if sent_etag is not None:
        request_headers["If-None-Match"] = sent_etag
    if sent_modified is not None:
        request_headers["If-Modified-Since"] = sent_modified
    client_headers = {"User-Agent": user_agent}
    if headers:
        client_headers.update(headers)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=client_headers,
        ) as client:
            async with client.stream("GET", url, headers=request_headers) as response:
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
                final_url = str(response.url)
                body = await _read_limited(response)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeEncodeError) as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return _error(message)
    if body is None:
        return _error(f"response exceeds {MAX_FEED_BYTES} bytes")
    try:
        # BytesIO: feedparser.parse(bytes) opens a body that is an existing path.
        def _parse_all() -> tuple[feedparser.FeedParserDict, tuple[int | None, list[int], list[str]]]:
            parsed = feedparser.parse(io.BytesIO(body))
            version = parsed.get("version") or ""
            hints = _feed_hints(body) if version.startswith("rss") else (None, [], [])
            return parsed, hints

        parsed, hints = await asyncio.to_thread(_parse_all)
    except Exception as exc:
        return _error(f"unable to parse feed: {exc}")
    return _from_parsed(
        parsed,
        base_url=final_url,
        etag=response_etag,
        last_modified=response_modified,
        entry_cap=entry_cap,
        hints=hints,
    )


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.base_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parsed = {key: (value or "") for key, value in attrs}
        if tag == "base":
            if self.base_href is None:
                href = parsed.get("href", "").strip()
                if href:
                    self.base_href = href
            return
        if tag != "link":
            return
        rel = parsed.get("rel", "").lower()
        mime = parsed.get("type", "").lower()
        href = parsed.get("href", "").strip()
        if rel == "alternate" and href and mime in ("application/rss+xml", "application/atom+xml"):
            self.links.append((href, "atom" if "atom" in mime else "rss"))


_PROBE_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")
_MAX_CANDIDATES = 50


async def _parse_feed_body(body: bytes) -> feedparser.FeedParserDict | None:
    # BytesIO: feedparser.parse(bytes) opens a body that is an existing path.
    parsed = await asyncio.to_thread(feedparser.parse, io.BytesIO(body))
    if parsed.version or parsed.entries:
        return parsed
    return None


def _feed_type(parsed: feedparser.FeedParserDict) -> str:
    return "atom" if (parsed.version or "").startswith("atom") else "rss"


async def discover_feeds(url: str, *, user_agent: str, timeout: float = 15.0) -> list[dict]:
    """Find feed URLs for a site: advertised <link rel=alternate> tags first,
    then common feed paths. Returns [{url, type, title?}] candidates."""
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(feed_url: str, feed_type: str | None, title: str | None) -> None:
        if feed_url in seen or len(candidates) >= _MAX_CANDIDATES:
            return
        seen.add(feed_url)
        item: dict = {"url": feed_url}
        if feed_type:
            item["type"] = feed_type
        if title:
            item["title"] = title
        candidates.append(item)

    try:
        client_ctx = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeEncodeError):
        return []
    async with client_ctx as client:

        async def bounded_fetch(target: str) -> tuple[bytes, str] | None:
            try:
                async with client.stream("GET", target) as response:
                    response.raise_for_status()
                    body = await _read_limited(response)
                    if body is None:
                        return None
                    return body, str(response.url)
            except (httpx.HTTPError, httpx.InvalidURL, UnicodeEncodeError):
                return None

        fetched = await bounded_fetch(url)
        if fetched is None:
            return []
        body, base = fetched
        # feedparser is the authority on "is this a feed"; HTML yields None.
        parsed = await _parse_feed_body(body)
        if parsed is not None:
            add(base, _feed_type(parsed), _text(parsed.feed.get("title")))
            return candidates
        collector = _LinkCollector()
        try:
            await asyncio.to_thread(collector.feed, body.decode("utf-8", errors="replace"))
        except Exception:
            pass
        doc_base = urljoin(base, collector.base_href) if collector.base_href else base
        for href, feed_type in collector.links:
            add(urljoin(doc_base, href), feed_type, None)
        if candidates:
            return candidates

        async def probe(path: str) -> tuple[str, str, str | None] | None:
            result = await bounded_fetch(urljoin(base, path))
            if result is None:
                return None
            probe_body, probe_url = result
            probe_parsed = await _parse_feed_body(probe_body)
            if probe_parsed is None:
                return None
            return probe_url, _feed_type(probe_parsed), _text(probe_parsed.feed.get("title"))

        for found in await asyncio.gather(*(probe(path) for path in _PROBE_PATHS)):
            if found is not None:
                add(*found)
        return candidates
