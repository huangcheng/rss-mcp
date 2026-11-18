from __future__ import annotations

import argparse
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from xml.etree.ElementTree import ParseError

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from rss_mcp import __version__
from rss_mcp.config import Settings, load_settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import fetch_feed
from rss_mcp.models import Entry, Subscription
from rss_mcp.opml import build_opml, parse_opml
from rss_mcp.readability_fetch import fetch_article_text
from rss_mcp.store import Store


@dataclass
class _App:
    settings: Settings
    store: Store


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limit(settings: Settings, limit: int | None) -> int:
    if limit is None:
        return settings.default_limit
    if limit < 0:
        return 0
    if limit > settings.max_limit:
        return settings.max_limit
    return limit


def _app(ctx: Context) -> _App:
    state = ctx.request_context.lifespan_context
    if not isinstance(state, _App):
        raise ToolError("database is not open")
    return state


def _app_lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[_App]:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(settings.db_path)
        try:
            init_db(conn)
            yield _App(settings=settings, store=Store(conn))
        finally:
            conn.close()

    return lifespan


async def _refresh(app: _App, sub: Subscription) -> dict[str, Any]:
    result = await fetch_feed(
        sub.url,
        etag=sub.etag,
        last_modified=sub.last_modified,
        user_agent=app.settings.user_agent,
        entry_cap=app.settings.fetch_entry_cap,
        timeout=30.0,
    )
    if result.status == "error":
        message = result.error or "fetch failed"
        app.store.update_fetch_meta(
            sub.id,
            etag=sub.etag,
            last_modified=sub.last_modified,
            last_fetched_at=sub.last_fetched_at,
            last_error=message,
        )
        return {
            "subscription_id": sub.id,
            "status": "error",
            "upserted": 0,
            "error": message,
        }
    if result.status == "not_modified":
        app.store.update_fetch_meta(
            sub.id,
            etag=result.etag,
            last_modified=result.last_modified,
            last_fetched_at=_utc_now(),
            last_error=None,
        )
        return {
            "subscription_id": sub.id,
            "status": "not_modified",
            "upserted": 0,
            "error": None,
        }
    written = app.store.upsert_entries(sub.id, result.entries)
    app.store.update_fetch_meta(
        sub.id,
        etag=result.etag,
        last_modified=result.last_modified,
        last_fetched_at=_utc_now(),
        last_error=None,
    )
    feed_title = (result.feed_title or "").strip()
    if feed_title and not (sub.title or "").strip():
        app.store.set_title_if_empty(sub.id, feed_title)
    return {
        "subscription_id": sub.id,
        "status": "ok",
        "upserted": written,
        "error": None,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": sum(row["status"] == "ok" for row in results),
        "not_modified": sum(row["status"] == "not_modified" for row in results),
        "error": sum(row["status"] == "error" for row in results),
        "upserted": sum(row["upserted"] for row in results),
        "results": results,
    }


def _entry_payload(entry: Entry, *, full: bool) -> dict[str, Any]:
    payload = asdict(entry)
    if not full:
        payload["content"] = None
    return payload


# MCPServer always installs resource and prompt list handlers. v1 is tools-only,
# so those methods are removed before the first discover.
_SDK_RESOURCE_AND_PROMPT_METHODS = (
    "prompts/get",
    "prompts/list",
    "resources/list",
    "resources/read",
    "resources/templates/list",
)


class _StaticTokenVerifier:
    """Accepts exactly one shared bearer token (RSS_MCP_AUTH_TOKEN)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="rss-mcp", scopes=[])
        return None


def create_server(settings: Settings | None = None) -> MCPServer:
    resolved = load_settings() if settings is None else settings
    auth: dict[str, Any] = {}
    if resolved.auth_token:
        # Static bearer mode has no OAuth issuer; AuthSettings still requires a
        # URL. resource_server_url=None keeps the SDK from advertising OAuth
        # protected-resource metadata that no authorization server backs.
        auth = {
            "auth": AuthSettings(issuer_url="http://localhost/", resource_server_url=None),
            "token_verifier": _StaticTokenVerifier(resolved.auth_token),
        }
    server = MCPServer(
        "rss-mcp",
        version=__version__,
        subscriptions=False,
        lifespan=_app_lifespan(resolved),
        **auth,
    )
    handlers = server._lowlevel_server._request_handlers
    for method in _SDK_RESOURCE_AND_PROMPT_METHODS:
        handlers.pop(method, None)

    @server.tool()
    async def add_subscription(
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Add a feed by URL. Repeating a URL returns the existing subscription."""
        sub = _app(ctx).store.add_subscription(url, title=title, tags=tags)
        return asdict(sub)

    @server.tool()
    async def remove_subscription(subscription_id: int, *, ctx: Context) -> dict[str, Any]:
        """Delete a subscription and its cached entries."""
        return {"ok": _app(ctx).store.remove_subscription(subscription_id)}

    @server.tool()
    async def list_subscriptions(*, ctx: Context) -> dict[str, Any]:
        """List subscriptions with unread counts."""
        rows = _app(ctx).store.list_subscriptions()
        return {"subscriptions": [asdict(row) for row in rows]}

    @server.tool()
    async def fetch_feed(subscription_id: int | None = None, *, ctx: Context) -> dict[str, Any]:
        """Fetch one subscription, or every subscription when the id is omitted."""
        app = _app(ctx)
        if subscription_id is None:
            targets = app.store.list_subscriptions()
        else:
            sub = app.store.get_subscription(subscription_id)
            if sub is None:
                raise ToolError(f"subscription not found: {subscription_id}")
            targets = [sub]
        results = [await _refresh(app, sub) for sub in targets]
        return _summary(results)

    @server.tool()
    async def list_entries(
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        since: str | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """List cached entries (bodies omitted). Limit falls back to the server default and is clamped."""
        app = _app(ctx)
        rows = app.store.list_entries(
            subscription_id=subscription_id,
            unread_only=unread_only,
            limit=_limit(app.settings, limit),
            since=since,
        )
        return {"entries": [_entry_payload(row, full=False) for row in rows]}

    @server.tool()
    async def read_entry(
        entry_id: int,
        fetch_full: bool = False,
        mark_read: bool = False,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Return one entry. fetch_full replaces content only when article text is returned. mark_read sets read state."""
        app = _app(ctx)
        entry = app.store.get_entry(entry_id)
        if entry is None:
            raise ToolError(f"entry not found: {entry_id}")
        if fetch_full and entry.url:
            text = await fetch_article_text(entry.url, user_agent=app.settings.user_agent)
            if isinstance(text, str):
                app.store.update_entry_content(entry.id, text)
                refreshed = app.store.get_entry(entry.id)
                if refreshed is not None:
                    entry = refreshed
        if mark_read:
            app.store.mark_entries([entry_id], True)
            refreshed = app.store.get_entry(entry_id)
            if refreshed is not None:
                entry = refreshed
        return _entry_payload(entry, full=True)

    @server.tool()
    async def search_entries(
        query: str,
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Full-text search over cached title, summary, and content. Omits entry bodies; use read_entry for full content."""
        app = _app(ctx)
        rows = app.store.search_entries(
            query,
            subscription_id=subscription_id,
            unread_only=unread_only,
            limit=_limit(app.settings, limit),
        )
        return {"entries": [_entry_payload(row, full=False) for row in rows]}

    @server.tool()
    async def mark_entry(
        entry_ids: list[int],
        read: bool,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Mark entries read or unread. Unknown ids are skipped."""
        updated = _app(ctx).store.mark_entries(entry_ids, read)
        return {"updated": updated}

    @server.tool()
    async def import_opml(opml: str, *, ctx: Context) -> dict[str, Any]:
        """Add each OPML feed. Existing URLs are skipped."""
        try:
            feeds = parse_opml(opml)
        except ParseError as exc:
            raise ToolError("invalid OPML") from exc
        store = _app(ctx).store
        known = {sub.id for sub in store.list_subscriptions()}
        added = 0
        skipped = 0
        for url, title in feeds:
            sub = store.add_subscription(url, title=title)
            if sub.id in known:
                skipped += 1
            else:
                known.add(sub.id)
                added += 1
        return {"added": added, "skipped": skipped}

    @server.tool()
    async def export_opml(*, ctx: Context) -> dict[str, Any]:
        """Export subscriptions as an OPML document."""
        return {"opml": build_opml(_app(ctx).store.list_subscriptions())}

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="rss-mcp",
        description="Agent-only MCP server for RSS subscriptions (stdio by default).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for streamable-http (default: 8000)",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="Endpoint path for streamable-http (default: /mcp)",
    )
    args = parser.parse_args(argv)
    server = create_server()
    if args.transport == "stdio":
        server.run()
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
