from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import Client

from rss_mcp.config import Settings

TOOL_NAMES = [
    "add_subscription",
    "remove_subscription",
    "list_subscriptions",
    "fetch_feed",
    "list_entries",
    "read_entry",
    "search_entries",
    "mark_entry",
    "import_opml",
    "export_opml",
]


class _RunRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_main_defaults_to_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from rss_mcp import server

    recorder = _RunRecorder()
    monkeypatch.setattr(server, "create_server", lambda: recorder)
    server.main([])
    assert recorder.calls == [{}]


def test_main_streamable_http_passes_host_port_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from rss_mcp import server

    recorder = _RunRecorder()
    monkeypatch.setattr(server, "create_server", lambda: recorder)
    server.main(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
            "--path",
            "/rss",
        ]
    )
    assert recorder.calls == [
        {
            "transport": "streamable-http",
            "host": "0.0.0.0",
            "port": 9123,
            "streamable_http_path": "/rss",
        }
    ]


def test_main_streamable_http_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from rss_mcp import server

    recorder = _RunRecorder()
    monkeypatch.setattr(server, "create_server", lambda: recorder)
    server.main(["--transport", "streamable-http"])
    assert recorder.calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 8000,
            "streamable_http_path": "/mcp",
        }
    ]


def test_main_rejects_unknown_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from rss_mcp import server

    def fail() -> _RunRecorder:
        pytest.fail("create_server must not run for an invalid transport")

    monkeypatch.setattr(server, "create_server", fail)
    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "sse"])
    assert exc.value.code == 2


@asynccontextmanager
async def _http_endpoint(settings: Settings) -> AsyncIterator[str]:
    from rss_mcp.server import create_server

    app = create_server(settings).streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(10):
            while not uvicorn_server.started:
                await asyncio.sleep(0.01)
        port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_streamable_http_serves_tools_end_to_end(server_settings: Settings) -> None:
    async with _http_endpoint(server_settings) as url:
        async with Client(url) as client:
            listed = await client.list_tools()
            added = await client.call_tool(
                "add_subscription",
                {"url": "https://example.com/http.xml", "title": "HTTP"},
            )
            subs = await client.call_tool("list_subscriptions", {})

    assert [tool.name for tool in listed.tools] == TOOL_NAMES
    assert added.is_error is False
    assert added.structured_content["url"] == "https://example.com/http.xml"
    assert subs.is_error is False
    assert subs.structured_content["subscriptions"][0]["title"] == "HTTP"
    assert server_settings.db_path.is_file()


_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "0"},
}


def _modern_headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


@pytest.mark.asyncio
async def test_modern_era_serves_tools_per_request_without_session(
    server_settings: Settings,
) -> None:
    # 2026-07-28: one self-contained POST per request, _meta envelope, no
    # initialize handshake and no Mcp-Session-Id.
    async with _http_endpoint(server_settings) as url:
        async with httpx.AsyncClient() as http:
            listed = await http.post(
                url,
                headers=_modern_headers("tools/list"),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {"_meta": _MODERN_META},
                },
            )
            called = await http.post(
                url,
                headers=_modern_headers("tools/call", "add_subscription"),
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "add_subscription",
                        "arguments": {"url": "https://example.com/modern.xml"},
                        "_meta": _MODERN_META,
                    },
                },
            )

    assert listed.status_code == 200
    assert listed.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in listed.headers
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == TOOL_NAMES
    assert called.status_code == 200
    assert (
        called.json()["result"]["structuredContent"]["url"]
        == "https://example.com/modern.xml"
    )
    assert server_settings.db_path.is_file()


@pytest.mark.asyncio
async def test_modern_era_rejects_mismatched_headers_and_unsupported_version(
    server_settings: Settings,
) -> None:
    # Spec MUSTs: header/body mismatch -> 400 + -32020 HeaderMismatch;
    # unserved version -> 400 + -32022 listing supported versions.
    async with _http_endpoint(server_settings) as url:
        async with httpx.AsyncClient() as http:
            mismatch = await http.post(
                url,
                headers=_modern_headers("tools/call", "wrong_name"),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "add_subscription",
                        "arguments": {"url": "https://example.com/mismatch.xml"},
                        "_meta": _MODERN_META,
                    },
                },
            )
            unsupported = await http.post(
                url,
                headers={
                    **_modern_headers("tools/list"),
                    "MCP-Protocol-Version": "1999-01-01",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            **_MODERN_META,
                            "io.modelcontextprotocol/protocolVersion": "1999-01-01",
                        }
                    },
                },
            )

    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == -32020
    assert unsupported.status_code == 400
    error = unsupported.json()["error"]
    assert error["code"] == -32022
    assert "2026-07-28" in error["data"]["supported"]