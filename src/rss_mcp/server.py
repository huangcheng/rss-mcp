from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from xml.etree.ElementTree import ParseError

import anyio
from lxml import html as lxml_html
from mcp.server import MCPServer
from opentelemetry import trace as otel_trace
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import ResourceUpdated
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from rss_mcp import __version__
from rss_mcp.config import Settings, load_settings
from rss_mcp.db import connect_db, init_db
from rss_mcp.feed_client import discover_feeds, fetch_feed
from rss_mcp.logging_setup import configure_logging, request_context
from rss_mcp.metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from rss_mcp.metrics import render_from_settings
from rss_mcp.models import Entry, Subscription
from rss_mcp.opml import build_opml, parse_opml
from rss_mcp.readability_fetch import fetch_article_text
from rss_mcp.status import status_payload
from rss_mcp.store import Store
from rss_mcp.telemetry import setup_telemetry, shutdown_telemetry

logger = logging.getLogger("rss_mcp")
_tracer = otel_trace.get_tracer("rss_mcp")

_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600
# PG fetch_interval is a 32-bit INTEGER; larger values also overflow datetime.
_MAX_FETCH_INTERVAL = 2_147_483_647


def _validate_fetch_interval(fetch_interval: int | None) -> None:
    if fetch_interval is not None and fetch_interval > _MAX_FETCH_INTERVAL:
        raise ToolError(f"fetch_interval must be at most {_MAX_FETCH_INTERVAL} seconds")


@dataclass
class _App:
    settings: Settings
    store: Store
    # Subscription bus for notifications/resources/updated; set when the
    # server serves subscriptions/listen so refreshes can push to listeners.
    events: InMemorySubscriptionBus | None = None


_UNREAD_URI = "rss://entries/unread"
_SUBSCRIPTIONS_URI = "rss://subscriptions"


async def _notify(app: _App, *uris: str) -> None:
    """Push notifications/resources/updated for the given URIs.

    Delivery is best-effort: nobody listening is fine, and a broken listener
    must never fail the mutation that triggered the notification.
    """
    if app.events is None:
        return
    for uri in uris:
        try:
            await app.events.publish(ResourceUpdated(uri=uri))
        except Exception:
            logger.exception("failed to publish resource update notification for %s", uri)


def _delivered_cutoff(replay_days: int) -> str:
    """Deliveries after this instant stay hidden; older ones may replay."""
    return (datetime.now(timezone.utc) - timedelta(days=replay_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_iso_timestamp(value: str, *, param: str) -> str:
    """Parse model-supplied ISO 8601 input into the stored canonical format.

    Stored timestamps are compared lexicographically, so raw pass-through of
    short or garbage values would silently match the wrong rows (e.g. a
    non-parseable "before" sorts above everything and sweeps the whole
    library). Reject anything unparseable at the tool boundary instead.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ToolError(f"invalid {param} timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"invalid {param} timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _backoff_seconds(failure_count: int) -> int:
    return min(_BACKOFF_BASE_SECONDS * 2 ** max(0, failure_count - 1), _BACKOFF_CAP_SECONDS)


def _last_fetched(sub: Subscription) -> datetime | None:
    if not sub.last_fetched_at:
        return None
    try:
        return datetime.strptime(sub.last_fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _in_backoff(sub: Subscription, now: datetime) -> bool:
    if sub.failure_count <= 0:
        return False
    last = _last_fetched(sub)
    if last is None:
        return False
    return now < last + timedelta(seconds=_backoff_seconds(sub.failure_count))


def _in_interval(sub: Subscription, now: datetime) -> bool:
    # Explicit fetch_interval wins; otherwise honor the feed's RSS ttl (minutes).
    # Failing feeds keep the backoff cadence (mirrors the scheduler policy).
    if sub.last_error:
        return False
    interval = sub.fetch_interval or (sub.ttl * 60 if sub.ttl else None)
    if not interval:
        return False
    last = _last_fetched(sub)
    if last is None:
        return False
    return now < last + timedelta(seconds=interval)


_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _in_skip_window(sub: Subscription, now: datetime) -> bool:
    # RSS 2.0 skipHours/skipDays are GMT hints; now is UTC. Weekday via index,
    # not strftime("%A"), which is locale-dependent.
    if now.hour in sub.skip_hours:
        return True
    return _DAY_NAMES[now.weekday()] in sub.skip_days


def _prune(app: _App) -> int:
    days = app.settings.retention_days
    if not days:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return app.store.prune_entries(cutoff)


async def _refresh(app: _App, sub: Subscription) -> dict[str, Any]:
    from urllib.parse import urlsplit

    host = urlsplit(sub.url).hostname or sub.url
    with _tracer.start_as_current_span(
        "rss.feed_fetch",
        attributes={
            "rss.subscription.id": sub.id,
            "rss.feed.host": host,
        },
    ) as span:
        result = await fetch_feed(
            sub.url,
            etag=sub.etag,
            last_modified=sub.last_modified,
            user_agent=sub.user_agent or app.settings.user_agent,
            headers=sub.headers,
            entry_cap=app.settings.fetch_entry_cap,
            timeout=30.0,
        )
        span.set_attribute("rss.fetch.status", result.status)
        if result.error:
            span.set_attribute("rss.fetch.error", result.error)
        row = await _record_refresh(app, sub, result)
        span.set_attribute("rss.fetch.upserted", row["upserted"])
        return row


async def _record_refresh(app: _App, sub: Subscription, result) -> dict[str, Any]:
    if result.status == "error":
        message = result.error or "fetch failed"
        failures = app.store.record_fetch_failure(sub.id, message)
        threshold = app.settings.disable_after_failures
        disabled = threshold > 0 and failures >= threshold
        if disabled:
            app.store.set_disabled(sub.id, True)
        return {
            "subscription_id": sub.id,
            "status": "error",
            "upserted": 0,
            "error": message,
            "disabled": disabled,
        }
    app.store.reset_failures(sub.id)
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
    written = app.store.upsert_entries(
        sub.id,
        result.entries,
        include_keywords=sub.include_keywords,
        exclude_keywords=sub.exclude_keywords,
    )
    app.store.update_fetch_meta(
        sub.id,
        etag=result.etag,
        last_modified=result.last_modified,
        last_fetched_at=_utc_now(),
        last_error=None,
        item_count=len(result.entries),
    )
    if (result.ttl, result.skip_hours, result.skip_days) != (sub.ttl, sub.skip_hours, sub.skip_days):
        app.store.update_feed_hints(
            sub.id,
            ttl=result.ttl,
            skip_hours=result.skip_hours,
            skip_days=result.skip_days,
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


def _skipped(sub: Subscription, reason: str) -> dict[str, Any]:
    return {
        "subscription_id": sub.id,
        "status": "skipped",
        "upserted": 0,
        "error": None,
        "reason": reason,
    }


async def _refresh_all(app: _App) -> dict[str, Any]:
    with _tracer.start_as_current_span("rss.refresh_all") as round_span:
        summary = await _refresh_all_inner(app)
        round_span.set_attribute("rss.round.total", len(summary["results"]))
        for key in ("ok", "not_modified", "error", "skipped", "upserted"):
            round_span.set_attribute(f"rss.round.{key}", summary[key])
        return summary


async def _refresh_all_inner(app: _App) -> dict[str, Any]:
    subs = app.store.list_subscriptions()
    now = datetime.now(timezone.utc)
    results: dict[int, dict[str, Any]] = {}
    limiter = anyio.CapacityLimiter(app.settings.fetch_concurrency)

    async def worker(sub: Subscription) -> None:
        async with limiter:
            results[sub.id] = await _refresh(app, sub)

    async with anyio.create_task_group() as tg:
        for sub in subs:
            if sub.disabled:
                results[sub.id] = _skipped(sub, "disabled")
            elif _in_backoff(sub, now):
                results[sub.id] = _skipped(sub, "backoff")
            elif _in_interval(sub, now):
                results[sub.id] = _skipped(sub, "interval")
            elif _in_skip_window(sub, now):
                results[sub.id] = _skipped(sub, "skip_hint")
            else:
                tg.start_soon(worker, sub)
    summary = _summary([results[sub.id] for sub in subs])
    # Fetch metadata (last_fetched_at, health) changes on every completed
    # pass; new entries additionally change the unread resource.
    if summary["upserted"] > 0:
        await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
    else:
        await _notify(app, _SUBSCRIPTIONS_URI)
    return summary


_PRUNE_INTERVAL_SECONDS = 3600


async def _scheduler_loop(app: _App) -> None:
    interval = app.settings.refresh_interval or 0
    while interval > 0:
        await anyio.sleep(interval)
        try:
            await _refresh_all(app)
        except Exception:
            logger.exception("scheduled refresh failed")


async def _prune_loop(app: _App) -> None:
    # Runs on its own cadence so retention is enforced regardless of which
    # driver owns fetching (in-process loop, Temporal, or manual tool calls).
    # Uses a dedicated connection: the lifespan connection is shared with
    # the main event loop, and interleaved transactions on PG can silently
    # make the DELETE see stale rows (hit in production 2026-11-20).
    while True:
        await anyio.sleep(_PRUNE_INTERVAL_SECONDS)
        conn = None
        try:
            # No init_db here: the schema is already migrated by the lifespan,
            # and running migrations every hour takes ACCESS EXCLUSIVE on every
            # table, which is exactly how a leaked read transaction turned into
            # a hung /healthz (2026-11-21).
            conn = connect_db(app.settings, autocommit=True)
            store = Store(conn)
            removed = _prune(_App(settings=app.settings, store=store))
            # Record every cycle, including no-ops: a silent zero-match DELETE
            # is exactly what status and /metrics must be able to expose.
            store.record_prune(removed)
            if removed > 0:
                logger.info("prune loop removed %d expired entries", removed)
                await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
        except Exception:
            logger.exception("scheduled prune failed")
        finally:
            if conn is not None:
                conn.close()


def _app_lifespan(settings: Settings, holder: dict[str, _App], events: InMemorySubscriptionBus | None):
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[_App]:
        setup_telemetry(settings)
        # Migrations run on their own transactional connection: init_schema
        # pairs DDL with conditional follow-ups and relies on commit/rollback
        # being real (an autocommit connection turns those into no-ops).
        migration_conn = connect_db(settings)
        try:
            init_db(migration_conn)
        finally:
            migration_conn.close()
        # The serving connection is autocommit: psycopg keeps a transaction
        # open after the first SELECT, and such a connection holds ACCESS
        # SHARE on everything it read, blocking later DDL (2026-11-21).
        conn = connect_db(settings, autocommit=True)
        try:
            app = _App(settings=settings, store=Store(conn), events=events)
            holder["app"] = app
            if settings.refresh_interval or settings.retention_days:
                async with anyio.create_task_group() as tg:
                    if settings.refresh_interval:
                        tg.start_soon(_scheduler_loop, app)
                    if settings.retention_days:
                        tg.start_soon(_prune_loop, app)
                    try:
                        yield app
                    finally:
                        tg.cancel_scope.cancel()
            else:
                yield app
        finally:
            holder.pop("app", None)
            conn.close()
            shutdown_telemetry()

    return lifespan


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": sum(row["status"] == "ok" for row in results),
        "not_modified": sum(row["status"] == "not_modified" for row in results),
        "error": sum(row["status"] == "error" for row in results),
        "skipped": sum(row["status"] == "skipped" for row in results),
        "upserted": sum(row["upserted"] for row in results),
        "results": results,
    }


_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _clean_snippet(snippet: str | None) -> str | None:
    # Real parser, not regex: handles entities, attributes containing '>',
    # unclosed tags, and script/style bodies. Residual angle brackets from
    # mid-tag snippet windows are neutralized so the plain-text contract holds.
    if not snippet or not snippet.strip():
        return None
    try:
        root = lxml_html.fromstring(snippet)
        for bad in list(root.iter("script", "style")):
            bad.drop_tree()
        text = root.text_content()
    except Exception:
        text = _HTML_TAG.sub("", snippet)
    text = text.replace("<", "").replace(">", "")
    return _WHITESPACE.sub(" ", text).strip() or None


def _entry_payload(entry: Entry, *, full: bool) -> dict[str, Any]:
    payload = asdict(entry)
    if not full:
        payload["content"] = None
    payload["snippet"] = _clean_snippet(entry.snippet)
    return payload


def _remediation_hint(sub: Subscription) -> str | None:
    if not sub.last_error and not sub.disabled:
        return None
    if sub.disabled:
        return (
            "auto-disabled after repeated failures; fetch_feed(subscription_id) retries and "
            "re-enables it, or remove_subscription to drop it"
        )
    error = (sub.last_error or "").lower()
    # TLS first: OpenSSL messages embed line numbers that look like statuses.
    if "ssl" in error or "certificate" in error or "tls" in error:
        return "TLS/certificate problem at the source; retry later or remove_subscription"
    # Anchor to the producer's exact "HTTP {code}" shape; bare substrings
    # misfire on parse-error line numbers and URLs.
    if error.startswith(("http 401", "http 403")):
        return (
            "likely bot protection; set a browser user_agent via update_subscription, "
            "or remove_subscription"
        )
    if error.startswith(("http 404", "http 410")):
        return "feed URL is gone; remove_subscription then add_subscription with the new url"
    return "transient source error; retry with fetch_feed(subscription_id)"


class _StaticTokenVerifier:
    """Accepts any of the configured shared bearer tokens."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        for known in self._tokens:
            if secrets.compare_digest(token, known):
                fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
                return AccessToken(token=token, client_id=f"rss-mcp-{fingerprint}", scopes=[])
        return None


class _RequestLogContext:
    """Tag every log line emitted while handling a request."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        fields: dict[str, Any] = {
            "http_method": scope.get("method"),
            "http_path": scope.get("path"),
        }
        if headers.get("mcp-method"):
            fields["mcp_method"] = headers["mcp-method"]
        if headers.get("mcp-name"):
            fields["mcp_name"] = headers["mcp-name"]
        token = request_context.set(fields)
        try:
            await self._app(scope, receive, send)
        finally:
            request_context.reset(token)


class _RateLimitMiddleware:
    """Sliding-window per-client request cap; keys on bearer token or client IP."""

    def __init__(self, app: Any, per_minute: int) -> None:
        self._app = app
        self._per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self._per_minute <= 0:
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        client = scope.get("client") or ("unknown", 0)
        key_source = headers.get(b"authorization") or str(client[0]).encode()
        key = hashlib.sha256(key_source).hexdigest()[:16]
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self._per_minute:
            response = JSONResponse({"error": "rate limit exceeded"}, status_code=429)
            await response(scope, receive, send)
            return
        hits.append(now)
        await self._app(scope, receive, send)


async def _healthz(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    try:
        conn = connect_db(settings, autocommit=True)
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM subscriptions").fetchone()["n"]
        finally:
            conn.close()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__, "subscriptions": count})


async def _metrics_endpoint(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    expected = settings.metrics_token
    if not expected:
        # Opt-in surface: unset token means the route does not exist.
        return PlainTextResponse("not found\n", status_code=404)
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        presented.strip().encode(), expected.encode()
    ):
        return PlainTextResponse(
            "unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
    # Rendering touches the database, and a scrape must never block the loop
    # or share the lifespan connection's transaction.
    body = await anyio.to_thread.run_sync(render_from_settings, settings)
    return Response(body, media_type=METRICS_CONTENT_TYPE)


def build_http_app(
    server: MCPServer,
    settings: Settings,
    *,
    path: str = "/mcp",
    host: str = "127.0.0.1",
) -> Any:
    app = server.streamable_http_app(streamable_http_path=path, host=host)
    app.state.settings = settings
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/metrics", _metrics_endpoint, methods=["GET"])
    return _RequestLogContext(_RateLimitMiddleware(app, settings.rate_limit_per_minute))


def create_server(settings: Settings | None = None) -> MCPServer:
    resolved = load_settings() if settings is None else settings
    holder: dict[str, _App] = {}
    auth: dict[str, Any] = {}
    tokens = resolved.tokens()
    if tokens:
        # Static bearer mode has no OAuth issuer; AuthSettings still requires a
        # URL. resource_server_url=None keeps the SDK from advertising OAuth
        # protected-resource metadata that no authorization server backs.
        auth = {
            "auth": AuthSettings(issuer_url="http://localhost/", resource_server_url=None),
            "token_verifier": _StaticTokenVerifier(tokens),
        }
    # Serving subscriptions/listen lets hosts wait for
    # notifications/resources/updated instead of polling; the bus is also the
    # seam refreshes publish to (see _notify).
    bus = InMemorySubscriptionBus()
    server = MCPServer(
        "rss-mcp",
        version=__version__,
        subscriptions=bus,
        lifespan=_app_lifespan(resolved, holder, bus),
        **auth,
    )

    def _resource_app() -> _App:
        app = holder.get("app")
        if app is None:
            raise ResourceError("database is not open")
        return app

    @server.resource(
        _SUBSCRIPTIONS_URI,
        name="subscriptions",
        description="All RSS subscriptions with unread counts and fetch health.",
        mime_type="application/json",
    )
    async def subscriptions_resource() -> str:
        app = _resource_app()
        return json.dumps(
            {"subscriptions": [asdict(sub) for sub in app.store.list_subscriptions()]},
            ensure_ascii=False,
        )

    @server.resource(
        _UNREAD_URI,
        name="unread_entries",
        description="Most recent unread entries across all subscriptions (bodies omitted, cross-feed duplicates excluded).",
        mime_type="application/json",
    )
    async def unread_entries_resource() -> str:
        app = _resource_app()
        rows = app.store.list_entries(unread_only=True, limit=app.settings.max_limit)
        return json.dumps(
            {"entries": [_entry_payload(row, full=False) for row in rows]},
            ensure_ascii=False,
        )

    @server.prompt(
        name="daily-digest",
        title="Daily digest",
        description="Brief the user on unread feed entries using the get_digest tool.",
    )
    async def daily_digest(
        max_entries: Annotated[int, Field(description="Entry cap passed to get_digest.")] = 20,
        tag: Annotated[str | None, Field(description="Restrict the digest to one tag.")] = None,
    ) -> str:
        scope = f'tag "{tag}"' if tag else "all subscriptions"
        return (
            "Brief the user on their unread RSS/Atom entries.\n"
            "1. Call the get_digest tool with "
            + (f'tag="{tag}" and ' if tag else "")
            + f"max_entries={max_entries}.\n"
            f"2. Cover {scope}: for each subscription in the digest give its title and unread"
            " count, then one line per notable entry (title and URL). Base every line on the"
            " returned summaries — do not invent details; entry bodies are omitted from the"
            " digest.\n"
            "3. Close by offering to open any entry in full with read_entry, and to catch up"
            " afterwards with mark_entry (all=True) — sweeps spare starred entries.\n"
            "4. If the digest has no unread entries, say so in one sentence and stop."
        )

    @server.tool()
    async def add_subscription(
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        muted: bool = False,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        fetch_interval: int | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Add a feed by URL. Repeating a URL returns the existing subscription. Optional per-feed user_agent/headers override the defaults. include_keywords/exclude_keywords hide entries whose title or summary does not match (or does match, case-insensitive substring); muted keeps the feed fetched but hides it from aggregate views. fetch_interval (seconds, max 2147483647) sets a per-feed minimum between refreshes; when unset or 0 the feed's RSS ttl applies, else the global cadence."""
        app = _app(ctx)
        _validate_fetch_interval(fetch_interval)
        sub = app.store.add_subscription(
            url,
            title=title,
            tags=tags,
            user_agent=user_agent,
            headers=headers,
            muted=muted,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            fetch_interval=fetch_interval,
        )
        await _notify(app, _SUBSCRIPTIONS_URI)
        return asdict(sub)

    @server.tool()
    async def update_subscription(
        subscription_id: int,
        title: str | None = None,
        tags: list[str] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        disabled: bool | None = None,
        muted: bool | None = None,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        fetch_interval: int | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Edit a subscription. Omitted fields are unchanged; empty string/dict/list clears a field. disabled=False re-enables and resets the failure count. Changing keyword rules re-applies them to already cached entries. fetch_interval sets a per-feed minimum seconds between refreshes (max 2147483647); 0 clears it, leaving the feed's RSS ttl or the global cadence in effect."""
        app = _app(ctx)
        _validate_fetch_interval(fetch_interval)
        if app.store.get_subscription(subscription_id) is None:
            raise ToolError(f"subscription not found: {subscription_id}")
        app.store.update_subscription(
            subscription_id,
            title=title,
            tags=tags,
            user_agent=user_agent,
            headers=headers,
            disabled=disabled,
            muted=muted,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            fetch_interval=fetch_interval,
        )
        # Mute and keyword rules change which entries readers see.
        if muted is not None or include_keywords is not None or exclude_keywords is not None:
            await _notify(app, _SUBSCRIPTIONS_URI, _UNREAD_URI)
        else:
            await _notify(app, _SUBSCRIPTIONS_URI)
        return asdict(app.store.get_subscription(subscription_id))

    @server.tool()
    async def remove_subscription(subscription_id: int, *, ctx: Context) -> dict[str, Any]:
        """Delete a subscription and its cached entries."""
        app = _app(ctx)
        removed = app.store.remove_subscription(subscription_id)
        if removed:
            await _notify(app, _SUBSCRIPTIONS_URI, _UNREAD_URI)
        return {"ok": removed}

    @server.tool()
    async def list_subscriptions(
        tag: str | None = None,
        failing_only: bool = False,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """List subscriptions with unread counts, optionally filtered by an exact tag. failing_only returns feeds with errors or auto-disabled, each with a remediation hint."""
        rows = _app(ctx).store.list_subscriptions(tag=tag, failing_only=failing_only)
        payloads = []
        for row in rows:
            payload = asdict(row)
            hint = _remediation_hint(row)
            if hint is not None:
                payload["remediation"] = hint
            payloads.append(payload)
        return {"subscriptions": payloads}

    @server.tool()
    async def server_status(*, ctx: Context) -> dict[str, Any]:
        """Self-inspection: database counts, feed-health buckets (including feeds that fetch fine but store nothing), scheduling/retention configuration, last prune run, a reading section (pending/delivered_24h/per-tag pending), failing feeds, and warnings. Use it to answer "is this server healthy?" without shell access — an empty warnings list means nothing looks wrong."""
        app = _app(ctx)
        return status_payload(app.settings, app.store)

    @server.tool()
    async def fetch_feed(subscription_id: int | None = None, *, ctx: Context) -> dict[str, Any]:
        """Fetch one subscription, or every subscription concurrently when the id is omitted. Disabled feeds, feeds in failure backoff, feeds inside their fetch_interval or RSS ttl, and feeds in their RSS skipHours/skipDays window are skipped in bulk mode; fetching one by id re-enables it and bypasses the gates."""
        app = _app(ctx)
        if subscription_id is None:
            return await _refresh_all(app)
        sub = app.store.get_subscription(subscription_id)
        if sub is None:
            raise ToolError(f"subscription not found: {subscription_id}")
        if sub.disabled:
            app.store.update_subscription(subscription_id, disabled=False)
            sub = app.store.get_subscription(subscription_id)
        summary = _summary([await _refresh(app, sub)])
        if summary["upserted"] > 0:
            await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
        else:
            await _notify(app, _SUBSCRIPTIONS_URI)
        return summary

    @server.tool()
    async def list_entries(
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        since: str | None = None,
        include_duplicates: bool = False,
        tag: str | None = None,
        starred: bool | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """List cached entries (bodies omitted). Cross-feed duplicates are hidden unless include_duplicates is set. Filter by tag or starred state. since/before accept ISO 8601 timestamps. Limit falls back to the server default and is clamped."""
        app = _app(ctx)
        if since is not None:
            since = _normalize_iso_timestamp(since, param="since")
        rows = app.store.list_entries(
            subscription_id=subscription_id,
            unread_only=unread_only,
            limit=_limit(app.settings, limit),
            since=since,
            include_duplicates=include_duplicates,
            tag=tag,
            starred=starred,
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
            if app.store.mark_entries([entry_id], True):
                refreshed = app.store.get_entry(entry_id)
                if refreshed is not None:
                    entry = refreshed
                await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
        return _entry_payload(entry, full=True)

    @server.tool()
    async def search_entries(
        query: str,
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        include_duplicates: bool = False,
        tag: str | None = None,
        since: str | None = None,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Full-text search over cached title, summary, and content, ranked by bm25 with a plain-text match snippet. Filter by tag or since (ISO 8601). Cross-feed duplicates are hidden unless include_duplicates is set. Omits entry bodies; use read_entry for full content."""
        app = _app(ctx)
        if since is not None:
            since = _normalize_iso_timestamp(since, param="since")
        rows = app.store.search_entries(
            query,
            subscription_id=subscription_id,
            unread_only=unread_only,
            limit=_limit(app.settings, limit),
            include_duplicates=include_duplicates,
            tag=tag,
            since=since,
        )
        return {"entries": [_entry_payload(row, full=False) for row in rows]}

    @server.tool()
    async def get_digest(
        since: str | None = None,
        tag: str | None = None,
        max_entries: int = 50,
        per_feed: int = 3,
        include_delivered: bool = False,
        replay_days: int | None = None,
        mark_read: bool = False,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """One-call unread briefing: per-subscription unread groups plus the most recent unread entries (deduped, mute- and filter-aware, bodies omitted; since accepts ISO 8601). per_feed caps each source's contribution (default 3) so high-volume feeds cannot monopolize the list. Entries already returned by an earlier digest are skipped so repeated calls show what is new; set include_delivered or replay_days=N to see them again, and mark_read=True to mark exactly what this digest returned as read (starred entries are kept). Follow up with read_entry for detail."""
        app = _app(ctx)
        if since is not None:
            since = _normalize_iso_timestamp(since, param="since")
        if replay_days is not None and replay_days < 0:
            raise ToolError("replay_days must be zero or greater")
        limit = _limit(app.settings, max_entries)
        cap = max(1, min(per_feed, limit))
        active = [sub for sub in app.store.list_subscriptions(tag=tag) if not sub.muted]
        # Cutoff for hiding already-delivered entries: None disables the
        # filter, "" hides every delivery, otherwise only deliveries newer
        # than the replay window are hidden.
        if include_delivered:
            delivered_cutoff = None
        elif replay_days is None:
            delivered_cutoff = ""
        else:
            delivered_cutoff = _delivered_cutoff(replay_days)
        # Per-source queries keep the pool diverse. Duplicates are fetched and
        # resolved at assembly so a copy can stand in when its keeper sits
        # deeper than the per-feed window in another feed.
        pool: list[Entry] = []
        for sub in active:
            # No zero-unread skip here: a feed whose only unread entries are
            # duplicate copies (unread_count 0) must still contribute stand-ins.
            pool.extend(
                app.store.list_entries(
                    subscription_id=sub.id,
                    unread_only=True,
                    since=since,
                    limit=cap,
                    include_duplicates=True,
                    hide_delivered_since=delivered_cutoff,
                )
            )
        pool.sort(key=lambda entry: (entry.published_at or "", entry.id), reverse=True)
        entries: list[Entry] = []
        taken_articles: set[int] = set()
        per_feed_counts: dict[int, int] = {}
        for entry in pool:
            if len(entries) >= limit:
                break
            if per_feed_counts.get(entry.subscription_id, 0) >= cap:
                continue
            article = entry.duplicate_of if entry.duplicate_of is not None else entry.id
            if article in taken_articles:
                continue
            taken_articles.add(article)
            per_feed_counts[entry.subscription_id] = per_feed_counts.get(entry.subscription_id, 0) + 1
            entries.append(entry)
        if entries:
            # Record every pool row whose article was handed out, not just the
            # rows returned: a duplicate copy that lost the assembly dedupe
            # would otherwise resurface as the same article on the next call.
            delivered_articles = set(taken_articles)
            app.store.record_deliveries(
                [
                    entry.id
                    for entry in pool
                    if (entry.duplicate_of if entry.duplicate_of is not None else entry.id)
                    in delivered_articles
                ],
                refresh=replay_days is not None,
            )
        marked = 0
        if mark_read:
            # Starred entries mean "keep for later": a digest must not eat them.
            marked = app.store.mark_entries(
                [entry.id for entry in entries if not entry.starred], True
            )
            if marked:
                await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
        visible = {sub.id for sub in active}
        pending = sum(
            count
            for subscription_id, count in app.store.unread_counts(
                exclude_delivered=True
            ).items()
            if subscription_id in visible
        )
        return {
            "generated_at": _utc_now(),
            "tag": tag,
            "since": since,
            "delivered": len(entries),
            "marked_read": marked,
            "pending": pending,
            "total_unread": sum(sub.unread_count for sub in active),
            "subscriptions": [
                {
                    "id": sub.id,
                    "title": sub.title,
                    "tags": sub.tags,
                    "unread_count": sub.unread_count,
                }
                for sub in active
                if sub.unread_count > 0
            ],
            "entries": [_entry_payload(entry, full=False) for entry in entries],
        }

    @server.tool()
    async def mark_entry(
        entry_ids: list[int] | None = None,
        subscription_id: int | None = None,
        read: bool = True,
        tag: str | None = None,
        before: str | None = None,
        all: bool = False,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Mark entries read or unread: by id list, a whole subscription, a tag, entries published before an ISO 8601 timestamp, or the whole library with all=True (catch-up). Unknown ids are skipped. Subscription/tag/before/all sweeps skip starred entries; use entry_ids to change those."""
        app = _app(ctx)
        has_ids = bool(entry_ids)
        if before is not None:
            before = _normalize_iso_timestamp(before, param="before")
        sweep = tag is not None or before is not None or all
        if (has_ids or subscription_id is not None) and sweep:
            raise ToolError("tag/before/all cannot be combined with entry_ids or subscription_id")
        if sweep:
            updated = app.store.mark_entries_bulk(tag=tag, before=before, read=read)
        elif subscription_id is not None:
            if app.store.get_subscription(subscription_id) is None:
                raise ToolError(f"subscription not found: {subscription_id}")
            updated = app.store.mark_subscription_entries(subscription_id, read)
            if has_ids:
                updated += app.store.mark_entries(entry_ids, read)
        elif has_ids:
            updated = app.store.mark_entries(entry_ids, read)
        else:
            raise ToolError("provide entry_ids, subscription_id, tag, before, or all")
        if updated:
            await _notify(app, _UNREAD_URI, _SUBSCRIPTIONS_URI)
        return {"updated": updated}

    @server.tool()
    async def star_entry(
        entry_ids: list[int],
        starred: bool = True,
        *, ctx: Context,
    ) -> dict[str, Any]:
        """Star (pin) or unstar entries. Starred entries are spared by catch-up sweeps and retention pruning. Unknown ids are skipped."""
        app = _app(ctx)
        updated = app.store.set_starred(entry_ids, starred)
        if updated:
            await _notify(app, _UNREAD_URI)
        return {"updated": updated}

    @server.tool()
    async def import_opml(opml: str, *, ctx: Context) -> dict[str, Any]:
        """Add each OPML feed, preserving folder names as tags. Existing URLs are skipped."""
        try:
            feeds = parse_opml(opml)
        except ParseError as exc:
            raise ToolError("invalid OPML") from exc
        app = _app(ctx)
        known = {sub.id for sub in app.store.list_subscriptions()}
        added = 0
        skipped = 0
        for url, title, tags in feeds:
            sub = app.store.add_subscription(url, title=title, tags=tags or None)
            if sub.id in known:
                skipped += 1
            else:
                known.add(sub.id)
                added += 1
        if added:
            await _notify(app, _SUBSCRIPTIONS_URI)
        return {"added": added, "skipped": skipped}

    @server.tool()
    async def export_opml(*, ctx: Context) -> dict[str, Any]:
        """Export subscriptions as an OPML document, grouped by first tag."""
        return {"opml": build_opml(_app(ctx).store.list_subscriptions())}

    @server.tool()
    async def discover_feed(url: str, *, ctx: Context) -> dict[str, Any]:
        """Find RSS/Atom feed URLs for a site: advertised link tags first, then common feed paths. Pass a candidate url to add_subscription."""
        app = _app(ctx)
        candidates = await discover_feeds(url, user_agent=app.settings.user_agent)
        return {"candidates": candidates}

    return server


def _serve_options(parser: Any) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    # `serve` is the default command, so the historical flag-only invocation
    # (`rss-mcp --transport streamable-http ...`, the Docker CMD) keeps
    # working without hand-scanning argv: argparse itself knows which tokens
    # are flag values. The serve flags live on a shared parent so both
    # `rss-mcp --port 9000` and `rss-mcp serve --port 9000` parse.
    common = argparse.ArgumentParser(add_help=False)
    _serve_options(common)
    parser = argparse.ArgumentParser(
        prog="rss-mcp",
        description="Agent-only MCP server for RSS subscriptions (stdio by default).",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "serve",
        parents=[common],
        help="Run the MCP server (default).",
    )
    subparsers.add_parser(
        "status",
        help="Print server self-inspection (database counts, feed health, warnings) as JSON.",
    )
    export = subparsers.add_parser(
        "export-db",
        help="Write the library as JSON Lines (- for stdout).",
    )
    export.add_argument("--out", default="-", help="Output path, or - for stdout (default).")
    export.add_argument(
        "--since", default=None, help="Only export entries published at or after this ISO 8601 time."
    )
    export.add_argument(
        "--no-content",
        action="store_true",
        help="Omit stored article bodies (smaller, summaries kept).",
    )
    load = subparsers.add_parser(
        "import-db",
        help="Load a JSON Lines export (- for stdin).",
    )
    load.add_argument("--in", dest="input_file", default="-", help="Input path, or - for stdin.")
    load.add_argument(
        "--replace",
        action="store_true",
        help="Delete the existing library first instead of merging by URL/guid.",
    )
    parser.set_defaults(command="serve")
    return parser


@contextmanager
def _output_stream(path: str) -> Iterator[Any]:
    if path == "-":
        yield sys.stdout
        return
    with open(path, "w", encoding="utf-8") as handle:
        yield handle


def _run_export(settings: Settings, args: Any) -> None:
    from rss_mcp.portability import export_lines

    since = None
    if args.since:
        try:
            since = _normalize_iso_timestamp(args.since, param="since")
        except ToolError as exc:
            raise SystemExit(f"rss-mcp: {exc}") from None
    counts = {"subscriptions": 0, "entries": 0}
    conn = connect_db(settings)
    try:
        init_db(conn)
        with _output_stream(args.out) as stream:
            for line in export_lines(
                conn, since=since, include_content=not args.no_content
            ):
                record_type = json.loads(line).get("type")
                if record_type == "subscription":
                    counts["subscriptions"] += 1
                elif record_type == "entry":
                    counts["entries"] += 1
                stream.write(line + "\n")
            stream.flush()
    finally:
        conn.close()
    # Export writes JSONL to stdout when --out is "-": the summary goes to
    # stderr so a pipeline stays parsable.
    print(json.dumps({"export": counts}), file=sys.stderr)


def _run_import(settings: Settings, args: Any) -> None:
    from rss_mcp.portability import import_lines

    conn = connect_db(settings)
    try:
        init_db(conn)
        if args.input_file == "-":
            summary = import_lines(conn, sys.stdin, replace=args.replace)
        else:
            with open(args.input_file, encoding="utf-8") as handle:
                summary = import_lines(conn, handle, replace=args.replace)
    finally:
        conn.close()
    print(json.dumps({"import": summary}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    json_logs = configure_logging(os.environ.get("RSS_MCP_LOG_FORMAT"))
    settings = load_settings()
    if args.command == "export-db":
        _run_export(settings, args)
        return
    if args.command == "import-db":
        _run_import(settings, args)
        return
    if args.command == "status":
        conn = connect_db(settings)
        try:
            init_db(conn)
            payload = status_payload(settings, Store(conn))
        finally:
            conn.close()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    server = create_server(settings)
    if args.transport == "stdio":
        server.run()
    else:
        import uvicorn

        app = build_http_app(server, settings, path=args.path, host=args.host)
        # log_config=None keeps uvicorn from installing its own handlers over
        # the JSON one; without the JSON format uvicorn keeps its defaults.
        uvicorn.run(app, host=args.host, port=args.port, **({"log_config": None} if json_logs else {}))