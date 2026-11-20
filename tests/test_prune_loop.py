"""Retention prune runs on its own loop, independent of the refresh driver."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


@pytest.mark.asyncio
async def test_prune_loop_runs_without_refresh_interval(monkeypatch):
    """The bug this fixes: retention only ran inside the (disabled) scheduler."""
    from rss_mcp import server

    settings = Settings(
        db_path=Path("/tmp") / "prune-loop-test.db",
        user_agent="rss-mcp/test",
        retention_days=90,
        refresh_interval=None,  # Temporal owns fetch; no in-process loop
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://prune.example/feed")

    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_entries(sub.id, [
        EntryInput(guid="ancient", title="Old", published_at=old),
        EntryInput(guid="recent", title="New", published_at=fresh),
    ])
    conn.close()

    # Speed up the prune loop for testing.
    monkeypatch.setattr(server, "_PRUNE_INTERVAL_SECONDS", 0.1)

    async with Client(server.create_server(settings)) as client:
        await asyncio.sleep(0.3)
        subs = await client.call_tool("list_subscriptions", {"tag": "不存在"})
    # The prune loop must have removed the 180-day-old entry even though
    # refresh_interval is None (no in-process scheduler).
    conn = connect(settings.db_path)
    store = Store(conn)
    entries = {e.guid for e in store.list_entries(subscription_id=sub.id, limit=10)}
    conn.close()
    assert "ancient" not in entries
    assert "recent" in entries


import asyncio  # noqa: E402


@pytest.mark.asyncio
async def test_no_prune_loop_without_retention_days():
    from rss_mcp import server

    settings = Settings(
        db_path=Path("/tmp") / "no-prune-test.db",
        user_agent="rss-mcp/test",
        retention_days=None,
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://no-prune.example/feed")

    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_entries(sub.id, [EntryInput(guid="ancient2", title="Old", published_at=old)])
    conn.close()

    async with Client(server.create_server(settings)) as client:
        await asyncio.sleep(0.3)

    conn = connect(settings.db_path)
    store = Store(conn)
    entries = {e.guid for e in store.list_entries(subscription_id=sub.id, limit=10)}
    conn.close()
    assert "ancient2" in entries  # retention unset → keep everything


@pytest.mark.asyncio
async def test_scheduler_loop_no_longer_prunes(monkeypatch):
    """The scheduler loop must be fetch-only now; prune owns its own loop."""
    from rss_mcp import server

    settings = Settings(
        db_path=Path("/tmp") / "sched-no-prune.db",
        user_agent="rss-mcp/test",
        retention_days=90,
        refresh_interval=1,
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://sched.example/feed")

    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_entries(sub.id, [EntryInput(guid="ancient3", title="Old", published_at=old)])
    conn.close()

    async def fake_refresh_all(app):
        return {"ok": 0, "not_modified": 0, "error": 0, "skipped": 0, "upserted": 0, "results": []}

    pruned: list[int] = []
    real_prune = server._prune

    def spy_prune(app):
        result = real_prune(app)
        if result:
            pruned.append(result)
        return result

    monkeypatch.setattr(server, "_refresh_all", fake_refresh_all)
    monkeypatch.setattr(server, "_prune", spy_prune)
    monkeypatch.setattr(server, "_PRUNE_INTERVAL_SECONDS", 10**9)  # effectively disable

    async with Client(server.create_server(settings)):
        await asyncio.sleep(0.5)

    # The scheduler ran refreshes, but pruning must NOT have been triggered
    # by the scheduler loop (it's now on _prune_loop's schedule).
    assert pruned == []

@pytest.mark.asyncio
async def test_prune_loop_runs_no_schema_migrations(monkeypatch):
    """Schema DDL every hour takes ACCESS EXCLUSIVE and can block behind reads."""
    from rss_mcp import server

    settings = Settings(
        db_path=Path("/tmp") / "prune-no-ddl.db",
        user_agent="rss-mcp/test",
        retention_days=90,
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    conn.close()

    ddl_calls: list[int] = []
    real_init_db = server.init_db

    def spy(conn):
        ddl_calls.append(1)
        return real_init_db(conn)

    monkeypatch.setattr(server, "init_db", spy)
    monkeypatch.setattr(server, "_PRUNE_INTERVAL_SECONDS", 0.05)

    async with Client(server.create_server(settings)):
        await asyncio.sleep(0.4)

    # One call from the lifespan; none from the prune cycles.
    assert len(ddl_calls) == 1
