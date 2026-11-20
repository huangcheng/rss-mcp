"""Prometheus exposition: rendering, auth gate, and failure behaviour."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.metrics import render_from_settings, render_metrics
from rss_mcp.models import EntryInput
from rss_mcp.status import status_payload
from rss_mcp.store import Store

_LINE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-zA-Z_][a-zA-Z0-9_]*=\"[^\"]*\"(,[a-zA-Z_][a-zA-Z0-9_]*=\"[^\"]*\")*\})?"
    r" -?[0-9.eE+]+$"
)


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(db_path=tmp_path / "rss.db", user_agent="rss-mcp/test", **kwargs)


def _open(settings: Settings) -> tuple[object, Store]:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _samples(body: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        assert _LINE.match(line), f"invalid exposition line: {line!r}"
        name, _, value = line.rpartition(" ")
        samples[name] = float(value)
    return samples


def test_render_reports_state_and_host_series(tmp_path):
    settings = _settings(tmp_path, retention_days=30)
    conn, store = _open(settings)
    sub = store.add_subscription("https://news.example/feed")
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=stamp,
        last_error=None,
        item_count=1,
    )
    store.upsert_entries(sub.id, [EntryInput(guid="one", title="One")])
    store.record_prune(4)
    body = render_metrics(settings, store)
    conn.close()

    samples = _samples(body)
    assert samples["rss_mcp_up"] == 1
    assert samples['rss_mcp_info{version="0.6.0",engine="sqlite"}'] == 1
    assert samples['rss_mcp_subscriptions{state="ok"}'] == 1
    assert samples['rss_mcp_subscriptions{state="failing"}'] == 0
    assert samples['rss_mcp_entries{state="unread"}'] == 1
    assert samples['rss_mcp_feed_last_success_timestamp_seconds{host="news.example"}'] > 0
    assert samples['rss_mcp_feed_consecutive_failures{host="news.example"}'] == 0
    assert samples["rss_mcp_prune_removed_total"] == 4
    assert samples["rss_mcp_prune_last_run_timestamp_seconds"] > 0
    assert samples["rss_mcp_retention_days"] == 30
    assert samples["rss_mcp_status_warnings"] == 0


def test_empty_feeds_are_an_overlay_not_a_sixth_state(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    ok = store.add_subscription("https://has-items.example/feed")
    store.upsert_entries(ok.id, [EntryInput(guid="one", title="One")])
    store.update_fetch_meta(
        ok.id,
        etag=None,
        last_modified=None,
        last_fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_error=None,
        item_count=1,
    )
    empty = store.add_subscription("https://empty.example/feed")
    store.update_fetch_meta(
        empty.id,
        etag=None,
        last_modified=None,
        last_fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_error=None,
        item_count=0,
    )
    body = render_metrics(settings, store)
    payload = status_payload(settings, store)
    conn.close()

    samples = _samples(body)
    states = sum(
        value
        for name, value in samples.items()
        if name.startswith("rss_mcp_subscriptions{")
    )
    assert states == payload["feeds"]["total"] == 2
    assert samples["rss_mcp_feeds_empty"] == 1
    assert all('state="empty"' not in name for name in samples)


def test_render_omits_prune_series_until_recorded(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://fresh.example/feed")
    body = render_metrics(settings, store)
    conn.close()

    assert "rss_mcp_prune_last_run_timestamp_seconds" not in body
    assert "rss_mcp_prune_removed_total" not in body


def test_render_counts_warnings(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    sub = store.add_subscription("https://warn.example/feed")
    store.record_fetch_failure(sub.id, "HTTP 500")
    samples = _samples(render_metrics(settings, store))
    conn.close()

    # The failing feed; a library younger than a day is not "stalled" yet.
    assert samples["rss_mcp_status_warnings"] == 1


def test_render_from_settings_reports_down_when_database_is_unreachable(tmp_path):
    # A directory is not a SQLite database: the scrape must degrade to up=0
    # instead of returning a 500 that Prometheus records as a missing target.
    settings = Settings(db_path=tmp_path, user_agent="rss-mcp/test")
    body = render_from_settings(settings)
    assert "rss_mcp_up 0" in body


@pytest.mark.asyncio
async def test_metrics_route_requires_its_own_token(tmp_path):
    from rss_mcp.server import build_http_app, create_server

    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://route.example/feed")
    conn.close()

    # No token configured: the route does not exist.
    app = build_http_app(create_server(settings), settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/metrics")).status_code == 404

    settings = _settings(tmp_path, metrics_token="scrape-me")
    app = build_http_app(create_server(settings), settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/metrics")).status_code == 401
        assert (
            await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        response = await client.get(
            "/metrics", headers={"Authorization": "Bearer scrape-me"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "rss_mcp_up 1" in response.text