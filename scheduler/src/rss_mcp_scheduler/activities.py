from __future__ import annotations

from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from rss_mcp_scheduler.config import SchedulerSettings

_PROTOCOL_VERSION = "2026-07-28"
_META = {
    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "rss-mcp-scheduler", "version": "0.1.0"},
}


class McpActivities:
    """Thin MCP HTTP client; fetch_feed is idempotent so retries are safe."""

    def __init__(self, settings: SchedulerSettings) -> None:
        self._settings = settings

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
            "Accept": "application/json, text/event-stream",
        }
        if self._settings.mcp_token:
            headers["Authorization"] = f"Bearer {self._settings.mcp_token}"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": _META},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(self._settings.mcp_url, json=body, headers=headers)
        if response.status_code in (401, 403):
            raise ApplicationError(
                f"MCP endpoint rejected credentials: HTTP {response.status_code}",
                non_retryable=True,
            )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise ApplicationError(f"MCP error: {data['error']}")
        result = data["result"]
        if result.get("isError"):
            text = " ".join(
                block.get("text", "") for block in result.get("content", []) if isinstance(block, dict)
            )
            raise ApplicationError(f"tool error: {text}", non_retryable=True)
        return result.get("structuredContent") or {}

    @activity.defn(name="list_subscriptions")
    async def list_subscriptions(self) -> list[dict[str, Any]]:
        payload = await self._call_tool("list_subscriptions", {})
        return payload.get("subscriptions", [])

    @activity.defn(name="fetch_feed")
    async def fetch_feed(self, subscription_id: int) -> dict[str, Any]:
        payload = await self._call_tool("fetch_feed", {"subscription_id": subscription_id})
        results = payload.get("results", [])
        if not results:
            raise ApplicationError("fetch_feed returned no results")
        row = results[0]
        if row.get("status") == "error":
            # Retryable: Temporal's retry policy owns the backoff schedule.
            raise ApplicationError(f"feed fetch failed: {row.get('error')}")
        return row