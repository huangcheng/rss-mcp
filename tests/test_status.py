"""Server self-inspection: status payload, maintenance records, CLI dispatch."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp import Client

from rss_mcp import server as server_module
from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.models import EntryInput
from rss_mcp.status import PRUNE_STALE_SECONDS, status_payload
from rss_mcp.store import Store


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(db_path=tmp_path / "rss.db", user_agent="rss-mcp/test", **kwargs)


def _open(settings: Settings) -> tuple[object, Store]:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _stamp(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_stats_counts_every_bucket(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    sub = store.add_subscription("https://status.example/feed")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="read", title="Read one"),
            EntryInput(guid="unread", title="Unread one"),
            EntryInput(guid="starred", title="Starred one"),
        ],
    )
    entries = {entry.guid: entry for entry in store.list_entries(subscription_id=sub.id, limit=10)}
    store.mark_entries([entries["read"].id], read=True)
    store.set_starred([entries["starred"].id], starred=True)
    stats = store.stats()
    conn.close()

    assert stats["subscriptions"] == 1
    assert stats["entries"] == 3
    assert stats["entries_unread"] == 2
    assert stats["entries_starred"] == 1
    assert stats["entries_duplicates"] == 0
    assert stats["entries_filtered"] == 0


def test_record_prune_accumulates_totals(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.record_prune(5)
    payload = store.record_prune(7)
    stored = store.get_meta("prune")
    conn.close()

    assert payload["last_removed"] == 7
    assert payload["total_removed"] == 12
    assert json.loads(stored["value"])["total_removed"] == 12


def test_status_payload_reports_health_and_warnings(tmp_path):
    settings = _settings(tmp_path, retention_days=30)
    conn, store = _open(settings)
    ok_sub = store.add_subscription("https://ok.example/feed")
    store.update_fetch_meta(
        ok_sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(minutes=-5)),
        last_error=None,
        item_count=0,
    )
    bad_sub = store.add_subscription("https://bad.example/feed")
    store.record_fetch_failure(bad_sub.id, "HTTP 500")
    muted = store.add_subscription("https://muted.example/feed", muted=True)
    store.update_fetch_meta(
        muted.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(minutes=-5)),
        last_error=None,
        item_count=0,
    )
    store.add_subscription("https://idle.example/feed")
    store.record_prune(0)
    payload = status_payload(settings, store)
    conn.close()

    assert payload["database"]["engine"] == "sqlite"
    assert payload["database"]["subscriptions"] == 4
    assert payload["feeds"]["ok"] == 1
    assert payload["feeds"]["failing"] == 1
    assert payload["feeds"]["muted"] == 1
    assert payload["feeds"]["never_fetched"] == 1
    assert payload["fetching"]["driver"] == "external-or-manual"
    assert payload["retention"]["last_prune_removed"] == 0
    assert payload["failing_feeds"][0]["subscription_id"] == bad_sub.id
    # Fresh prune record + a successful fetch five minutes ago: nothing to warn
    # about beyond the failing feed.
    assert not any("prune" in warning for warning in payload["warnings"])
    assert payload["warnings"] == [
        "1 feed(s) failing; see list_subscriptions(failing_only=true)",
        "2 feed(s) fetch successfully but store no entries; "
        "the feed may be empty upstream or parse to nothing",
    ]


def test_status_warns_when_prune_is_stale_or_missing(tmp_path):
    settings = _settings(tmp_path, retention_days=30)
    conn, store = _open(settings)
    store.add_subscription("https://stale.example/feed")
    fresh = status_payload(settings, store)
    assert any("never recorded a run" in warning for warning in fresh["warnings"])

    old = datetime.now(timezone.utc) - timedelta(seconds=PRUNE_STALE_SECONDS + 60)
    store.record_prune(3, when=old.strftime("%Y-%m-%dT%H:%M:%SZ"))
    stale = status_payload(settings, store)
    conn.close()

    assert any("last prune ran" in warning for warning in stale["warnings"])


def test_status_warns_when_nothing_fetched_for_a_day(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    sub = store.add_subscription("https://quiet.example/feed")
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(days=-2)),
        last_error=None,
    )
    payload = status_payload(settings, store)
    conn.close()

    assert any("fetched successfully" in warning for warning in payload["warnings"])


def test_status_warns_when_nothing_ever_fetched(tmp_path):
    """A library older than a day with zero successful fetches is stalled."""
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://never.example/feed")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE subscriptions SET created_at = ?", (old,))
    conn.commit()
    payload = status_payload(settings, store)
    conn.close()

    assert any(
        "no feed has fetched successfully yet" in warning for warning in payload["warnings"]
    )


def test_fresh_library_is_not_warned_about_fetching(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://brand-new.example/feed")
    payload = status_payload(settings, store)
    conn.close()

    assert payload["warnings"] == []


def test_paused_feed_is_not_a_warning(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    sub = store.add_subscription("https://paused.example/feed")
    store.update_subscription(sub.id, disabled=True)
    payload = status_payload(settings, store)
    conn.close()

    assert payload["feeds"]["disabled"] == 1
    assert payload["warnings"] == []


def test_status_reports_in_process_driver(tmp_path):
    settings = _settings(tmp_path, refresh_interval=900)
    conn, store = _open(settings)
    store.add_subscription("https://driver.example/feed")
    payload = status_payload(settings, store)
    conn.close()

    assert payload["fetching"]["driver"] == "in-process"
    assert payload["fetching"]["refresh_interval_seconds"] == 900


def test_prune_staleness_threshold_tracks_the_loop_cadence():
    # PRUNE_STALE_SECONDS is a human threshold; it must stay a few missed
    # wakes of the real loop, not drift free of it.
    assert PRUNE_STALE_SECONDS >= 2 * server_module._PRUNE_INTERVAL_SECONDS


def test_prune_loop_records_every_cycle(tmp_path, monkeypatch):
    settings = _settings(tmp_path, retention_days=90)
    conn, store = _open(settings)
    sub = store.add_subscription("https://prune-meta.example/feed")
    old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_entries(sub.id, [EntryInput(guid="old", title="Old", published_at=old)])
    conn.close()

    monkeypatch.setattr(server_module, "_PRUNE_INTERVAL_SECONDS", 0.05)

    async def run() -> None:
        async with Client(server_module.create_server(settings)):
            # Several cycles: the first removes the entry, later ones record a
            # no-op — both must be visible to status.
            await asyncio.sleep(0.6)

    asyncio.run(run())

    conn, store = _open(settings)
    stored = json.loads(store.get_meta("prune")["value"])
    remaining = store.list_entries(subscription_id=sub.id, limit=10)
    conn.close()

    assert stored["last_removed"] == 0  # later cycles recorded a no-op
    assert stored["total_removed"] == 1
    assert remaining == []


@pytest.mark.asyncio
async def test_server_status_tool_exposes_payload(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://tool.example/feed")
    conn.close()

    async with Client(server_module.create_server(settings)) as client:
        result = await client.call_tool("server_status", {})

    payload = json.loads(result.content[0].text)
    assert payload["database"]["subscriptions"] == 1
    assert payload["feeds"]["total"] == 1
    assert isinstance(payload["warnings"], list)


def test_cli_keeps_flag_only_invocations_and_accepts_status(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    store.add_subscription("https://cli.example/feed")
    conn.close()

    parser = server_module.build_parser()
    # The historical flag-only form (also the Docker CMD) still means `serve`.
    parsed = parser.parse_args(["--transport", "streamable-http", "--port", "9000"])
    assert (parsed.command, parsed.transport, parsed.port) == ("serve", "streamable-http", 9000)
    # A flag value that happens to spell a subcommand stays a value.
    parsed = parser.parse_args(["--path", "status", "--host", "serve"])
    assert (parsed.command, parsed.path, parsed.host) == ("serve", "status", "serve")
    # `serve` also accepts its own flags, which is what CMD uses.
    assert parser.parse_args(["serve", "--port", "8001"]).port == 8001
    assert parser.parse_args(["status"]).command == "status"

    monkeypatch.setenv("RSS_MCP_DB", str(settings.db_path))
    server_module.main(["status"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["database"]["subscriptions"] == 1


def test_status_tolerates_an_unreadable_prune_record(tmp_path):
    settings = _settings(tmp_path, retention_days=30)
    conn, store = _open(settings)
    store.add_subscription("https://corrupt.example/feed")
    store.set_meta("prune", "not-json")
    payload = status_payload(settings, store)
    # A corrupt record cannot prove the loop ran, so it must not silence the
    # warning; the counter also restarts instead of raising.
    recorded = store.record_prune(2)
    conn.close()

    assert any("never recorded a run" in warning for warning in payload["warnings"])
    assert recorded["total_removed"] == 2
