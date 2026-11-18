from __future__ import annotations

import httpx
import pytest

from rss_mcp_scheduler.activities import McpActivities
from rss_mcp_scheduler.config import SchedulerSettings
from temporalio.exceptions import ApplicationError


def _settings(**overrides) -> SchedulerSettings:
    base = dict(mcp_url="https://rss.example/mcp", mcp_token="sekrit")
    base.update(overrides)
    return SchedulerSettings(**base)


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rss_mcp_scheduler.activities.httpx.AsyncClient", factory)


def _tool_response(structured: dict, *, is_error: bool = False) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"isError": is_error, "structuredContent": structured, "content": []},
        },
    )


@pytest.mark.asyncio
async def test_list_subscriptions_sends_modern_envelope_and_bearer(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = __import__("json").loads(request.content)
        return _tool_response({"subscriptions": [{"id": 1, "url": "https://a.xml"}]})

    _install(monkeypatch, handler)
    acts = McpActivities(_settings())
    subs = await acts.list_subscriptions()

    assert subs == [{"id": 1, "url": "https://a.xml"}]
    assert seen["headers"]["mcp-protocol-version"] == "2026-07-28"
    assert seen["headers"]["mcp-method"] == "tools/call"
    assert seen["headers"]["mcp-name"] == "list_subscriptions"
    assert seen["headers"]["authorization"] == "Bearer sekrit"
    meta = seen["body"]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"


@pytest.mark.asyncio
async def test_fetch_feed_ok_returns_row(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_response(
            {"results": [{"subscription_id": 7, "status": "ok", "upserted": 3, "error": None}]}
        )

    _install(monkeypatch, handler)
    row = await McpActivities(_settings()).fetch_feed(7)
    assert row["status"] == "ok"
    assert row["upserted"] == 3


@pytest.mark.asyncio
async def test_fetch_feed_error_is_retryable_application_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_response(
            {"results": [{"subscription_id": 7, "status": "error", "upserted": 0, "error": "HTTP 502"}]}
        )

    _install(monkeypatch, handler)
    with pytest.raises(ApplicationError) as exc:
        await McpActivities(_settings()).fetch_feed(7)
    assert "HTTP 502" in str(exc.value)
    assert exc.value.non_retryable is False


@pytest.mark.asyncio
async def test_fetch_feed_skipped_passes_through(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_response(
            {"results": [{"subscription_id": 7, "status": "skipped", "reason": "backoff"}]}
        )

    _install(monkeypatch, handler)
    row = await McpActivities(_settings()).fetch_feed(7)
    assert row["status"] == "skipped"


@pytest.mark.asyncio
async def test_auth_rejection_is_non_retryable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    _install(monkeypatch, handler)
    with pytest.raises(ApplicationError) as exc:
        await McpActivities(_settings()).list_subscriptions()
    assert exc.value.non_retryable is True


@pytest.mark.asyncio
async def test_tool_error_is_non_retryable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "subscription not found: 99"}],
                },
            },
        )

    _install(monkeypatch, handler)
    with pytest.raises(ApplicationError) as exc:
        await McpActivities(_settings()).fetch_feed(99)
    assert exc.value.non_retryable is True
    assert "subscription not found" in str(exc.value)