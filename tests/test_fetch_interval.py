"""Per-feed fetch intervals: bulk gating, manual bypass, tool wiring."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import FetchResult
from rss_mcp.store import Store


def _seed(settings: Settings) -> Store:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    return Store(conn)


def _ok() -> FetchResult:
    return FetchResult(status="ok", entries=[], etag=None, last_modified=None, feed_title=None, error=None)


@pytest.mark.asyncio
async def test_bulk_fetch_respects_per_feed_interval(server_settings: Settings, monkeypatch) -> None:
    from rss_mcp import server

    store = _seed(server_settings)
    slow = store.add_subscription("https://weekly.example/f", fetch_interval=7 * 24 * 3600)
    fast = store.add_subscription("https://daily.example/f")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(slow.id, etag=None, last_modified=None, last_fetched_at=now, last_error=None)

    fetched_urls: list[str] = []

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        fetched_urls.append(url)
        return _ok()

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})

    rows = {r["subscription_id"]: r for r in summary.structured_content["results"]}
    assert rows[slow.id]["status"] == "skipped"
    assert rows[slow.id]["reason"] == "interval"
    assert rows[fast.id]["status"] == "ok"
    assert fetched_urls == ["https://daily.example/f"]


@pytest.mark.asyncio
async def test_manual_fetch_bypasses_interval(server_settings: Settings, monkeypatch) -> None:
    from rss_mcp import server

    store = _seed(server_settings)
    slow = store.add_subscription("https://weekly.example/f", fetch_interval=7 * 24 * 3600)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(slow.id, etag=None, last_modified=None, last_fetched_at=now, last_error=None)

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return _ok()

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        result = await client.call_tool("fetch_feed", {"subscription_id": slow.id})

    assert result.structured_content["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_interval_set_and_cleared_via_tools(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        sub = await client.call_tool(
            "add_subscription", {"url": "https://x.example/f", "fetch_interval": 3600}
        )
        assert sub.structured_content["fetch_interval"] == 3600
        sid = sub.structured_content["id"]

        updated = await client.call_tool("update_subscription", {"subscription_id": sid, "fetch_interval": 60})
        assert updated.structured_content["fetch_interval"] == 60

        cleared = await client.call_tool("update_subscription", {"subscription_id": sid, "fetch_interval": 0})
        assert cleared.structured_content["fetch_interval"] is None


@pytest.mark.asyncio
async def test_expired_interval_fetches_again(server_settings: Settings, monkeypatch) -> None:
    from rss_mcp import server

    store = _seed(server_settings)
    sub = store.add_subscription("https://hourly.example/f", fetch_interval=3600)
    store.update_fetch_meta(
        sub.id, etag=None, last_modified=None, last_fetched_at="2026-11-18T00:00:00Z", last_error=None
    )

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return _ok()

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})

    assert summary.structured_content["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_oversized_interval_rejected(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        added = await client.call_tool(
            "add_subscription", {"url": "https://huge.example/f", "fetch_interval": 10**12}
        )
        assert added.is_error is True
        sub = await client.call_tool("add_subscription", {"url": "https://ok2.example/f"})
        updated = await client.call_tool(
            "update_subscription",
            {"subscription_id": sub.structured_content["id"], "fetch_interval": 10**12},
        )
        assert updated.is_error is True


@pytest.mark.asyncio
async def test_first_bulk_fetch_with_interval_never_fetched(server_settings: Settings, monkeypatch) -> None:
    from rss_mcp import server

    store = _seed(server_settings)
    store.add_subscription("https://new.example/f", fetch_interval=604800)

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return _ok()

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await client.call_tool("fetch_feed", {})

    assert summary.structured_content["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_non_positive_interval_clears_on_both_paths(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        zero = await client.call_tool(
            "add_subscription", {"url": "https://zero.example/f", "fetch_interval": 0}
        )
        assert zero.structured_content["fetch_interval"] is None
        negative = await client.call_tool(
            "add_subscription", {"url": "https://neg.example/f", "fetch_interval": -3600}
        )
        assert negative.structured_content["fetch_interval"] is None
        sid = negative.structured_content["id"]
        updated = await client.call_tool(
            "update_subscription", {"subscription_id": sid, "fetch_interval": -3600}
        )
        assert updated.structured_content["fetch_interval"] is None