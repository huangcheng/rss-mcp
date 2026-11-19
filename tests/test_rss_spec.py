"""RSS 2.0 spec alignment: guid isPermaLink, enclosure scheme, ttl/skip hints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from mcp import Client

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import FetchResult, fetch_feed
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


def _seed(settings: Settings) -> Store:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    return Store(conn)


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rss_mcp.feed_client.httpx.AsyncClient", factory)


def _rss(items: str, channel_extra: str = "") -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Spec Feed</title><link>https://spec.example</link>"
        "<description>spec</description>" + channel_extra + items + "</channel></rss>"
    ).encode()


@pytest.mark.asyncio
async def test_guid_permalink_fallback_when_link_missing(monkeypatch):
    body = _rss(
        '<item><guid isPermaLink="true">https://spec.example/post-1</guid>'
        "<title>Permalink item</title></item>"
        '<item><guid isPermaLink="false">opaque-id-2</guid>'
        "<title>Opaque item</title></item>"
        "<item><guid>https://spec.example/post-3</guid>"
        "<title>Default permalink</title></item>"
        '<item><guid isPermaLink="true">urn:uuid:abc</guid>'
        "<title>Urn guid</title></item>"
        '<item><guid isPermaLink="true">HTTPS://spec.example/post-5</guid>'
        "<title>Uppercase permalink</title></item>"
        "<item><link>javascript:alert(1)</link>"
        '<guid isPermaLink="true">https://spec.example/post-6</guid>'
        "<title>Bad link filtered</title></item>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://spec.example/feed", etag=None, last_modified=None,
        user_agent="test", entry_cap=50,
    )
    by_title = {e.title: e for e in result.entries}
    assert by_title["Permalink item"].url == "https://spec.example/post-1"
    assert by_title["Opaque item"].url is None
    assert by_title["Default permalink"].url == "https://spec.example/post-3"
    assert by_title["Urn guid"].url is None
    assert by_title["Uppercase permalink"].url == "https://spec.example/post-5"
    # A present-but-non-http link is filtered; guid fallback only applies
    # when no link exists (feedparser guidislink semantics).
    assert by_title["Bad link filtered"].url is None


@pytest.mark.asyncio
async def test_enclosure_non_http_schemes_filtered(monkeypatch):
    body = _rss(
        "<item><title>Media</title>"
        '<enclosure url="https://cdn.example/a.mp3" length="100" type="audio/mpeg"/>'
        '<enclosure url="HTTP://cdn.example/c.mp3" length="100" type="audio/mpeg"/>'
        '<enclosure url="ftp://cdn.example/b.mp3" length="100" type="audio/mpeg"/>'
        '<enclosure url="javascript:alert(1)" length="1" type="text/html"/>'
        "</item>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://spec.example/feed", etag=None, last_modified=None,
        user_agent="test", entry_cap=50,
    )
    assert result.entries[0].enclosures == [
        {"url": "https://cdn.example/a.mp3", "type": "audio/mpeg", "length": "100"},
        {"url": "HTTP://cdn.example/c.mp3", "type": "audio/mpeg", "length": "100"},
    ]


@pytest.mark.asyncio
async def test_ttl_and_skip_hints_parsed(monkeypatch):
    body = _rss(
        "<item><title>One</title></item>",
        channel_extra="<ttl>60</ttl><skipHours><hour>3</hour><hour>4</hour><hour>4</hour>"
        "<hour>24</hour><hour>x</hour></skipHours>"
        "<skipDays><day>sunday</day></skipDays>",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://spec.example/feed", etag=None, last_modified=None,
        user_agent="test", entry_cap=50,
    )
    assert result.ttl == 60
    assert result.skip_hours == [3, 4]
    assert result.skip_days == ["Sunday"]


@pytest.mark.asyncio
async def test_invalid_ttl_and_dtd_body_yield_no_hints(monkeypatch):
    for extra, expected_ttl in [("<ttl>0</ttl>", None), ("<ttl>abc</ttl>", None),
                                ("<ttl>99999999999999</ttl>", None)]:
        body = _rss("<item><title>One</title></item>", channel_extra=extra)

        def handler(request: httpx.Request, body=body) -> httpx.Response:
            return httpx.Response(200, content=body)

        _install(monkeypatch, handler)
        result = await fetch_feed(
            "https://spec.example/feed", etag=None, last_modified=None,
            user_agent="test", entry_cap=50,
        )
        assert result.status == "ok"
        assert result.ttl is expected_ttl

    # DTD bodies skip hint parsing entirely (entity-expansion guard).
    dtd_body = (
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY t "60">]>'
        '<rss version="2.0"><channel><title>T</title><ttl>&t;</ttl>'
        "<item><title>One</title></item></channel></rss>"
    ).encode()

    def dtd_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=dtd_body)

    _install(monkeypatch, dtd_handler)
    result = await fetch_feed(
        "https://spec.example/feed", etag=None, last_modified=None,
        user_agent="test", entry_cap=50,
    )
    assert result.status == "ok"
    assert result.ttl is None


@pytest.mark.asyncio
async def test_ttl_gates_bulk_and_explicit_interval_overrides(server_settings: Settings, monkeypatch):
    from rss_mcp import server

    store = _seed(server_settings)
    hinted = store.add_subscription("https://ttl.example/f")
    store.update_feed_hints(hinted.id, ttl=60, skip_hours=[], skip_days=[])
    overridden = store.add_subscription("https://override.example/f", fetch_interval=1)
    store.update_feed_hints(overridden.id, ttl=60, skip_hours=[], skip_days=[])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(hinted.id, etag=None, last_modified=None, last_fetched_at=now, last_error=None)
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(overridden.id, etag=None, last_modified=None, last_fetched_at=earlier, last_error=None)

    fetched: list[str] = []

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        fetched.append(url)
        return FetchResult(status="ok", entries=[], etag=None, last_modified=None, feed_title=None, error=None)

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})

    rows = {r["subscription_id"]: r for r in summary.structured_content["results"]}
    assert rows[hinted.id]["status"] == "skipped"
    assert rows[hinted.id]["reason"] == "interval"
    assert rows[overridden.id]["status"] == "ok"
    assert fetched == ["https://override.example/f"]


@pytest.mark.asyncio
async def test_skip_hours_and_days_gate_bulk(server_settings: Settings, monkeypatch):
    import calendar

    from rss_mcp import server

    now = datetime.now(timezone.utc)
    store = _seed(server_settings)
    hourly = store.add_subscription("https://skip-hour.example/f")
    # Adjacent buckets so an hour rollover mid-test still matches.
    store.update_feed_hints(hourly.id, ttl=None, skip_hours=[now.hour, (now.hour + 1) % 24], skip_days=[])
    daily = store.add_subscription("https://skip-day.example/f")
    store.update_feed_hints(
        daily.id, ttl=None, skip_hours=[],
        skip_days=[calendar.day_name[now.weekday()], calendar.day_name[(now.weekday() + 1) % 7]],
    )
    normal = store.add_subscription("https://normal.example/f")

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(status="ok", entries=[], etag=None, last_modified=None, feed_title=None, error=None)

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})
        manual = await client.call_tool("fetch_feed", {"subscription_id": hourly.id})

    rows = {r["subscription_id"]: r for r in summary.structured_content["results"]}
    assert rows[hourly.id]["reason"] == "skip_hint"
    assert rows[daily.id]["reason"] == "skip_hint"
    assert rows[normal.id]["status"] == "ok"
    assert manual.structured_content["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_hints_stored_on_fetch_and_kept_on_304(server_settings: Settings, monkeypatch):
    from rss_mcp import server

    store = _seed(server_settings)
    sub = store.add_subscription("https://hints.example/f")
    responses = iter([
        FetchResult(status="ok", entries=[], etag="e1", last_modified=None,
                    feed_title=None, error=None, ttl=45, skip_hours=[2], skip_days=["Monday"]),
        FetchResult(status="not_modified", entries=[], etag="e1", last_modified=None,
                    feed_title=None, error=None),
    ])

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return next(responses)

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        await client.call_tool("fetch_feed", {"subscription_id": sub.id})
        stored = store.get_subscription(sub.id)
        assert stored.ttl == 45
        assert stored.skip_hours == [2]
        assert stored.skip_days == ["Monday"]
        await client.call_tool("fetch_feed", {"subscription_id": sub.id})

    stored = store.get_subscription(sub.id)
    assert stored.ttl == 45
    assert stored.skip_hours == [2]
    assert stored.skip_days == ["Monday"]


@pytest.mark.asyncio
async def test_failing_feed_with_ttl_keeps_backoff_cadence(server_settings: Settings, monkeypatch):
    from rss_mcp import server

    store = _seed(server_settings)
    sub = store.add_subscription("https://flaky-ttl.example/f")
    store.update_feed_hints(sub.id, ttl=1440, skip_hours=[], skip_days=[])
    store.record_fetch_failure(sub.id, "HTTP 502")
    # Rewind the attempt so the 60s backoff (failure_count=1) has expired.
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(sub.id, etag=None, last_modified=None, last_fetched_at=earlier, last_error="HTTP 502")

    fetched: list[str] = []

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        fetched.append(url)
        return FetchResult(status="ok", entries=[], etag=None, last_modified=None, feed_title=None, error=None)

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})

    row = summary.structured_content["results"][0]
    assert row["status"] == "ok", "failing feed must retry after backoff, not wait out the ttl"
    assert fetched == ["https://flaky-ttl.example/f"]