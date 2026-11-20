"""Incremental reading: digest deliveries, mark_read, pending counts, empty feeds."""

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
from rss_mcp.metrics import render_metrics
from rss_mcp.models import EntryInput
from rss_mcp.portability import export_lines, import_lines
from rss_mcp.status import status_payload
from rss_mcp.store import Store


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(db_path=tmp_path / "rss.db", user_agent="rss-mcp/test", **kwargs)


def _open(settings: Settings) -> tuple[object, Store]:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _stamp(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(settings: Settings, count: int = 5) -> dict[str, int]:
    conn, store = _open(settings)
    news = store.add_subscription("https://news.example/feed", title="News", tags=["news"])
    tech = store.add_subscription("https://tech.example/feed", title="Tech", tags=["tech"])
    for index in range(count):
        store.upsert_entries(
            news.id,
            [
                EntryInput(
                    guid=f"n{index}",
                    title=f"News {index}",
                    published_at=_stamp(timedelta(hours=-index)),
                )
            ],
        )
        store.upsert_entries(
            tech.id,
            [
                EntryInput(
                    guid=f"t{index}",
                    title=f"Tech {index}",
                    published_at=_stamp(timedelta(hours=-index, minutes=-30)),
                )
            ],
        )
    ids = {"news": news.id, "tech": tech.id}
    conn.close()
    return ids


@pytest.mark.asyncio
async def test_repeated_digests_are_disjoint(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, count=4)

    async with Client(server_module.create_server(settings)) as client:
        first = json.loads(
            (await client.call_tool("get_digest", {"per_feed": 2, "max_entries": 10})).content[0].text
        )
        second = json.loads(
            (await client.call_tool("get_digest", {"per_feed": 2, "max_entries": 10})).content[0].text
        )

    first_ids = {entry["id"] for entry in first["entries"]}
    second_ids = {entry["id"] for entry in second["entries"]}
    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)
    assert first["delivered"] == len(first_ids)
    assert second["pending"] < first["pending"]


@pytest.mark.asyncio
async def test_include_delivered_returns_the_same_entries(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, count=3)

    async with Client(server_module.create_server(settings)) as client:
        first = json.loads((await client.call_tool("get_digest", {})).content[0].text)
        again = json.loads(
            (
                await client.call_tool(
                    "get_digest", {"include_delivered": True, "per_feed": 3}
                )
            ).content[0].text
        )

    assert {entry["id"] for entry in first["entries"]} <= {
        entry["id"] for entry in again["entries"]
    }


@pytest.mark.asyncio
async def test_replay_days_reoffers_old_deliveries_only(tmp_path):
    settings = _settings(tmp_path)
    ids = _seed(settings, count=2)
    conn, store = _open(settings)
    entries = store.list_entries(subscription_id=ids["news"], limit=10)
    store.record_deliveries([entries[0].id], when=_stamp(timedelta(days=-3)))
    store.record_deliveries([entries[1].id], when=_stamp(timedelta(hours=-1)))
    conn.close()

    async with Client(server_module.create_server(settings)) as client:
        replayed = json.loads(
            (await client.call_tool("get_digest", {"replay_days": 1, "per_feed": 5})).content[0].text
        )

    replayed_ids = {entry["id"] for entry in replayed["entries"]}
    assert entries[0].id in replayed_ids  # delivered 3 days ago: replayed
    assert entries[1].id not in replayed_ids  # delivered an hour ago: still hidden


@pytest.mark.asyncio
async def test_mark_read_marks_exactly_what_was_delivered_and_spares_stars(tmp_path):
    settings = _settings(tmp_path)
    ids = _seed(settings, count=3)
    conn, store = _open(settings)
    starred = store.list_entries(subscription_id=ids["news"], limit=1)[0]
    store.set_starred([starred.id], True)
    conn.close()

    async with Client(server_module.create_server(settings)) as client:
        digest = json.loads(
            (await client.call_tool("get_digest", {"per_feed": 5, "mark_read": True})).content[0].text
        )

    conn, store = _open(settings)
    delivered = [store.get_entry(entry["id"]) for entry in digest["entries"]]
    starred_now = store.get_entry(starred.id)
    conn.close()

    assert digest["marked_read"] == sum(1 for entry in delivered if not entry.starred)
    assert all(entry.read_at is not None for entry in delivered if not entry.starred)
    assert starred_now is not None and starred_now.read_at is None


def test_deliveries_cascade_with_entries(tmp_path):
    settings = _settings(tmp_path)
    ids = _seed(settings, count=2)
    conn, store = _open(settings)
    entry = store.list_entries(subscription_id=ids["news"], limit=1)[0]
    store.record_deliveries([entry.id])
    store.remove_subscription(ids["news"])
    conn.close()

    conn, store = _open(settings)
    remaining = store.delivery_times([entry.id])
    conn.close()
    assert remaining == {}


def test_status_reading_reports_pending_per_tag(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, count=3)
    conn, store = _open(settings)
    payload = status_payload(settings, store)
    conn.close()

    assert payload["reading"]["pending"] == 6
    assert payload["reading"]["delivered_24h"] == 0
    assert payload["reading"]["by_tag"] == {"news": 3, "tech": 3}


def test_status_warns_about_feeds_that_store_nothing(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    empty = store.add_subscription("https://empty.example/feed")
    store.update_fetch_meta(
        empty.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(minutes=-5)),
        last_error=None,
        item_count=0,
    )
    payload = status_payload(settings, store)
    conn.close()

    assert payload["feeds"]["empty"] == 1
    assert any("store no entries" in warning for warning in payload["warnings"])


def test_metrics_expose_reading_gauges(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, count=2)
    conn, store = _open(settings)
    body = render_metrics(settings, store)
    conn.close()

    samples = {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if line and not line.startswith("#")
    }
    assert samples["rss_mcp_digest_pending"] == 4
    assert samples["rss_mcp_digest_delivered_24h"] == 0
    assert samples['rss_mcp_tag_pending{tag="news"}'] == 2
    assert samples['rss_mcp_tag_pending{tag="tech"}'] == 2


@pytest.mark.asyncio
async def test_replay_digest_does_not_reoffer_the_same_entry_forever(tmp_path):
    """A replay must move the delivery stamp, or the entry replays every call."""
    settings = _settings(tmp_path)
    ids = _seed(settings, count=1)
    conn, store = _open(settings)
    entry = store.list_entries(subscription_id=ids["news"], limit=1)[0]
    store.record_deliveries([entry.id], when=_stamp(timedelta(days=-10)))
    conn.close()

    async with Client(server_module.create_server(settings)) as client:
        first = json.loads(
            (await client.call_tool("get_digest", {"replay_days": 7, "per_feed": 5})).content[0].text
        )
        second = json.loads(
            (await client.call_tool("get_digest", {"replay_days": 7, "per_feed": 5})).content[0].text
        )

    assert entry.id in {item["id"] for item in first["entries"]}
    assert entry.id not in {item["id"] for item in second["entries"]}


@pytest.mark.asyncio
async def test_duplicate_copy_is_not_redelivered_as_the_same_article(tmp_path):
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    a = store.add_subscription("https://a.example/feed")
    b = store.add_subscription("https://b.example/feed")
    for sub in (a, b):
        store.upsert_entries(
            sub.id,
            [
                EntryInput(
                    guid="same",
                    url="https://shared.example/article",
                    title="Shared article",
                    published_at="2026-11-10T00:00:00Z",
                )
            ],
        )
    conn.close()

    async with Client(server_module.create_server(settings)) as client:
        first = json.loads((await client.call_tool("get_digest", {"per_feed": 5})).content[0].text)
        second = json.loads((await client.call_tool("get_digest", {"per_feed": 5})).content[0].text)

    def articles(payload):
        return {
            entry.get("duplicate_of") or entry["id"] for entry in payload["entries"]
        }

    assert articles(first)
    assert articles(second).isdisjoint(articles(first))


@pytest.mark.asyncio
async def test_mark_read_digest_pushes_an_unread_update(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, count=1)

    async with Client(server_module.create_server(settings)) as client:
        async with client.listen(resource_subscriptions=["rss://entries/unread"]) as stream:
            await client.call_tool("get_digest", {"mark_read": True, "per_feed": 5})
            event = await asyncio.wait_for(anext(stream), timeout=5)
    assert event.uri == "rss://entries/unread"


def test_retention_pruning_a_feed_does_not_make_it_look_empty(tmp_path):
    """The empty bucket tracks the last fetch, not the row count retention moves."""
    settings = _settings(tmp_path, retention_days=30)
    conn, store = _open(settings)
    sub = store.add_subscription("https://monthly.example/feed")
    old = _stamp(timedelta(days=-40))
    store.upsert_entries(
        sub.id, [EntryInput(guid="old", title="Old", published_at=old)]
    )
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(hours=-1)),
        last_error=None,
        item_count=1,
    )
    store.prune_entries(_stamp(timedelta(days=-30)))
    entries = store.list_entries(subscription_id=sub.id, limit=10)
    payload = status_payload(settings, store)
    conn.close()

    assert entries == []
    assert payload["feeds"]["empty"] == 0
    assert not any("store no entries" in warning for warning in payload["warnings"])


def test_export_import_keeps_delivery_state(tmp_path):
    source = _settings(tmp_path / "source")
    source.db_path.parent.mkdir(parents=True, exist_ok=True)
    ids = _seed(source, count=2)
    conn, store = _open(source)
    entry = store.list_entries(subscription_id=ids["news"], limit=1)[0]
    store.record_deliveries([entry.id], when=_stamp(timedelta(hours=-2)))
    lines = list(export_lines(conn))
    conn.close()

    target = _settings(tmp_path / "target")
    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn, store = _open(target)
    summary = import_lines(conn, lines)
    conn.close()

    conn, store = _open(target)
    delivered = store.delivery_times([entry.id])
    conn.close()
    assert summary["deliveries_restored"] == 1
    assert entry.id in delivered


def test_unrecorded_item_count_is_not_treated_as_empty(tmp_path):
    """NULL means "no parsed fetch yet"; only a recorded zero means empty."""
    settings = _settings(tmp_path)
    conn, store = _open(settings)
    sub = store.add_subscription("https://new.example/feed")
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(minutes=-5)),
        last_error=None,
    )
    unrecorded = status_payload(settings, store)
    sub = store.get_subscription(sub.id)
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at=_stamp(timedelta(minutes=-5)),
        last_error=None,
        item_count=0,
    )
    recorded_zero = status_payload(settings, store)
    conn.close()

    assert sub is not None and sub.last_item_count is None
    assert unrecorded["feeds"]["empty"] == 0
    assert recorded_zero["feeds"]["empty"] == 1
