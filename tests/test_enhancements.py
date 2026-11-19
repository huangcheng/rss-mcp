"""Tests for the 0.1.2 enhancements: concurrency, backoff/disable, scheduler,
retention, tags, bulk mark, snippets, enclosures, resources, multi-token,
rate limiting, and /healthz."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest
import uvicorn
from mcp import Client

from rss_mcp.config import Settings, load_settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import FetchResult
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        db_path=tmp_path / "enhance.db",
        user_agent="rss-mcp/test",
    )
    base.update(overrides)
    return Settings(**base)


def _open_store(settings: Settings) -> tuple[object, Store]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _ok_result(entries=None, etag="e", feed_title=None) -> FetchResult:
    return FetchResult(
        status="ok",
        entries=entries or [],
        etag=etag,
        last_modified=None,
        feed_title=feed_title,
        error=None,
    )


def _error_result(message="boom") -> FetchResult:
    return FetchResult(
        status="error",
        entries=[],
        etag=None,
        last_modified=None,
        feed_title=None,
        error=message,
    )


@pytest.mark.asyncio
async def test_bulk_fetch_runs_concurrently_within_the_cap(tmp_path, monkeypatch):
    from rss_mcp import server

    settings = _settings(tmp_path, fetch_concurrency=3)
    conn, store = _open_store(settings)
    for i in range(6):
        store.add_subscription(f"https://example.com/{i}.xml")
    conn.close()

    active = 0
    peak = 0

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await anyio.sleep(0.05)
        active -= 1
        return _ok_result()

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(settings)) as client:
        result = await client.call_tool("fetch_feed", {})

    assert result.structured_content["ok"] == 6
    assert peak > 1, "bulk fetch ran sequentially"
    assert peak <= 3, "concurrency cap exceeded"


@pytest.mark.asyncio
async def test_failures_back_off_then_auto_disable_and_manual_fetch_re_enables(tmp_path, monkeypatch):
    from rss_mcp import server

    settings = _settings(tmp_path, disable_after_failures=2)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/flaky.xml")
    conn.close()

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return _error_result("HTTP 502")

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(settings)) as client:
        first = await client.call_tool("fetch_feed", {"subscription_id": sub.id})
        assert first.structured_content["results"][0]["disabled"] is False

        bulk = await client.call_tool("fetch_feed", {})
        row = bulk.structured_content["results"][0]
        assert row["status"] == "skipped"
        assert row["reason"] == "backoff"

        second = await client.call_tool("fetch_feed", {"subscription_id": sub.id})
        assert second.structured_content["results"][0]["disabled"] is True

        bulk2 = await client.call_tool("fetch_feed", {})
        row2 = bulk2.structured_content["results"][0]
        assert row2["status"] == "skipped"
        assert row2["reason"] == "disabled"

        manual = await client.call_tool("fetch_feed", {"subscription_id": sub.id})
        assert manual.structured_content["results"][0]["status"] == "error"

    conn = connect(settings.db_path)
    stored = Store(conn).get_subscription(sub.id)
    conn.close()
    assert stored.disabled is False
    assert stored.failure_count == 1
    assert stored.last_error == "HTTP 502"


@pytest.mark.asyncio
async def test_scheduler_refreshes_and_prunes_on_interval(tmp_path, monkeypatch):
    from rss_mcp import server

    settings = _settings(tmp_path, refresh_interval=1, retention_days=30)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/sched.xml")
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="ancient", title="Ancient", published_at="2020-01-01T00:00:00Z"),
            EntryInput(guid="fresh", title="Fresh", published_at=recent),
        ],
    )
    ancient_id = next(e.id for e in store.list_entries(limit=10) if e.guid == "ancient")
    conn.execute("INSERT INTO entry_state (entry_id, starred) VALUES (?, 1)", (ancient_id,))
    conn.commit()
    conn.close()

    calls = []

    async def fake_refresh_all(app):
        calls.append(1)
        return {"ok": 0}

    monkeypatch.setattr(server, "_refresh_all", fake_refresh_all)
    async with Client(server.create_server(settings)):
        await asyncio.sleep(1.3)

    assert calls, "scheduler never fired"
    conn = connect(settings.db_path)
    remaining = {e.guid for e in Store(conn).list_entries(limit=10)}
    conn.close()
    # ancient is starred so it survives pruning; fresh is within the window
    assert remaining == {"ancient", "fresh"}


@pytest.mark.asyncio
async def test_import_opml_preserves_categories_as_tags_and_list_filters(tmp_path):
    from rss_mcp.server import create_server

    settings = _settings(tmp_path)
    opml = """<?xml version="1.0"?>
<opml version="2.0"><body>
  <outline text="News" title="News">
    <outline type="rss" xmlUrl="https://example.com/a.xml" title="A"/>
  </outline>
  <outline text="Tech">
    <outline type="rss" xmlUrl="https://example.com/b.xml" title="B"/>
  </outline>
</body></opml>"""
    async with Client(create_server(settings)) as client:
        await client.call_tool("import_opml", {"opml": opml})
        news = await client.call_tool("list_subscriptions", {"tag": "News"})
        tech = await client.call_tool("list_subscriptions", {"tag": "Tech"})
        everything = await client.call_tool("list_subscriptions", {})

    assert [s["url"] for s in news.structured_content["subscriptions"]] == ["https://example.com/a.xml"]
    assert news.structured_content["subscriptions"][0]["tags"] == ["News"]
    assert [s["url"] for s in tech.structured_content["subscriptions"]] == ["https://example.com/b.xml"]
    assert len(everything.structured_content["subscriptions"]) == 2


@pytest.mark.asyncio
async def test_mark_entry_by_subscription_and_validation(tmp_path):
    from rss_mcp.server import create_server

    settings = _settings(tmp_path)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/mark.xml")
    store.upsert_entries(sub.id, [EntryInput(guid=f"g{i}", title=f"T{i}") for i in range(3)])
    conn.close()

    async with Client(create_server(settings)) as client:
        marked = await client.call_tool("mark_entry", {"subscription_id": sub.id, "read": True})
        listed = await client.call_tool("list_subscriptions", {})
        unread = await client.call_tool("mark_entry", {"subscription_id": sub.id, "read": False})
        missing = await client.call_tool("mark_entry", {"subscription_id": 999, "read": True})
        empty = await client.call_tool("mark_entry", {"read": True})

    assert marked.structured_content == {"updated": 3}
    assert listed.structured_content["subscriptions"][0]["unread_count"] == 0
    assert unread.structured_content == {"updated": 3}
    assert missing.is_error is True
    # No selectors at all is an error; catch-up is explicit (all=True).
    assert empty.is_error is True


@pytest.mark.asyncio
async def test_update_subscription_edits_clears_and_re_enables(tmp_path):
    from rss_mcp.server import create_server

    settings = _settings(tmp_path)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/edit.xml")
    store.record_fetch_failure(sub.id, "HTTP 403")
    store.set_disabled(sub.id, True)
    conn.close()

    async with Client(create_server(settings)) as client:
        updated = await client.call_tool(
            "update_subscription",
            {
                "subscription_id": sub.id,
                "user_agent": "Mozilla/5.0 (custom)",
                "headers": {"Accept": "application/rss+xml"},
                "tags": ["tech"],
                "disabled": False,
            },
        )
        cleared = await client.call_tool(
            "update_subscription",
            {"subscription_id": sub.id, "user_agent": "", "headers": {}, "tags": []},
        )
        missing = await client.call_tool("update_subscription", {"subscription_id": 999})

    payload = updated.structured_content
    assert payload["user_agent"] == "Mozilla/5.0 (custom)"
    assert payload["headers"] == {"Accept": "application/rss+xml"}
    assert payload["tags"] == ["tech"]
    assert payload["disabled"] is False
    assert payload["failure_count"] == 0
    assert payload["last_error"] is None
    after = cleared.structured_content
    assert after["user_agent"] is None
    assert after["headers"] is None
    assert after["tags"] == []
    assert missing.is_error is True


@pytest.mark.asyncio
async def test_search_returns_match_snippet(tmp_path):
    from rss_mcp.server import create_server

    settings = _settings(tmp_path)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/snippet.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid="s1", title="Zephyr report", content="The zephyr winds blow strongly today.")],
    )
    conn.close()

    async with Client(create_server(settings)) as client:
        found = await client.call_tool("search_entries", {"query": "zephyr"})

    entry = found.structured_content["entries"][0]
    assert entry["snippet"] is not None
    assert "[zephyr]" in entry["snippet"].lower() or "[Zephyr]" in entry["snippet"]


@pytest.mark.asyncio
async def test_enclosures_round_trip_from_fetch_to_payload(tmp_path, monkeypatch):
    from rss_mcp import server

    settings = _settings(tmp_path)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/podcast.xml")
    conn.close()

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return _ok_result(
            entries=[
                EntryInput(
                    guid="ep1",
                    title="Episode 1",
                    enclosures=[{"url": "https://cdn.example.com/ep1.mp3", "type": "audio/mpeg"}],
                )
            ]
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(settings)) as client:
        await client.call_tool("fetch_feed", {"subscription_id": sub.id})
        listed = await client.call_tool("list_entries", {})
        entry_id = listed.structured_content["entries"][0]["id"]
        read = await client.call_tool("read_entry", {"entry_id": entry_id})

    assert read.structured_content["enclosures"] == [
        {"url": "https://cdn.example.com/ep1.mp3", "type": "audio/mpeg"}
    ]


@pytest.mark.asyncio
async def test_resources_expose_subscriptions_and_unread(tmp_path):
    from rss_mcp.server import create_server

    settings = _settings(tmp_path)
    conn, store = _open_store(settings)
    sub = store.add_subscription("https://example.com/res.xml", title="Res")
    store.upsert_entries(sub.id, [EntryInput(guid="r1", title="Unread item")])
    conn.close()

    async with Client(create_server(settings)) as client:
        listed = await client.list_resources()
        subs_payload = await client.read_resource("rss://subscriptions")
        unread_payload = await client.read_resource("rss://entries/unread")

    uris = {str(r.uri) for r in listed.resources}
    assert uris == {"rss://subscriptions", "rss://entries/unread"}
    subs = json.loads(subs_payload.contents[0].text)
    assert subs["subscriptions"][0]["url"] == "https://example.com/res.xml"
    unread = json.loads(unread_payload.contents[0].text)
    assert unread["entries"][0]["title"] == "Unread item"


@asynccontextmanager
async def _http_app_endpoint(settings: Settings) -> AsyncIterator[str]:
    from rss_mcp.server import build_http_app, create_server

    app = build_http_app(create_server(settings), settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(10):
            while not uvicorn_server.started:
                await asyncio.sleep(0.01)
        port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_healthz_reports_ok_without_auth(tmp_path):
    settings = _settings(tmp_path, auth_token="sekrit")
    conn, store = _open_store(settings)
    store.add_subscription("https://example.com/health.xml")
    conn.close()

    async with _http_app_endpoint(settings) as base:
        async with httpx.AsyncClient() as http:
            response = await http.get(f"{base}/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["subscriptions"] == 1


@pytest.mark.asyncio
async def test_multiple_tokens_are_accepted(tmp_path):
    settings = _settings(tmp_path, auth_tokens=("token-one", "token-two"))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    headers = {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Accept": "application/json, text/event-stream",
    }
    async with _http_app_endpoint(settings) as base:
        async with httpx.AsyncClient() as http:
            results = []
            for token in ("token-one", "token-two", "token-three"):
                r = await http.post(
                    f"{base}/mcp",
                    json=body,
                    headers={**headers, "Authorization": f"Bearer {token}"},
                )
                results.append(r.status_code)
    assert results == [200, 200, 401]


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_cap(tmp_path):
    settings = _settings(tmp_path, rate_limit_per_minute=3)
    async with _http_app_endpoint(settings) as base:
        async with httpx.AsyncClient() as http:
            statuses = [(await http.get(f"{base}/healthz")).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3:] == [429, 429]


def test_load_settings_parses_new_env_vars(monkeypatch):
    monkeypatch.setenv("RSS_MCP_AUTH_TOKENS", "a, b ,,c")
    monkeypatch.setenv("RSS_MCP_FETCH_CONCURRENCY", "4")
    monkeypatch.setenv("RSS_MCP_REFRESH_INTERVAL", "900")
    monkeypatch.setenv("RSS_MCP_DISABLE_AFTER_FAILURES", "3")
    monkeypatch.setenv("RSS_MCP_RETENTION_DAYS", "60")
    monkeypatch.setenv("RSS_MCP_RATE_LIMIT_PER_MINUTE", "30")
    s = load_settings()
    assert s.auth_tokens == ("a", "b", "c")
    assert s.fetch_concurrency == 4
    assert s.refresh_interval == 900
    assert s.disable_after_failures == 3
    assert s.retention_days == 60
    assert s.rate_limit_per_minute == 30
    assert s.tokens() == ["a", "b", "c"]


def test_load_settings_invalid_numbers_fall_back(monkeypatch):
    monkeypatch.setenv("RSS_MCP_FETCH_CONCURRENCY", "not-a-number")
    monkeypatch.setenv("RSS_MCP_REFRESH_INTERVAL", "-5")
    monkeypatch.setenv("RSS_MCP_RETENTION_DAYS", "abc")
    s = load_settings()
    assert s.fetch_concurrency == 8
    assert s.refresh_interval is None
    assert s.retention_days is None