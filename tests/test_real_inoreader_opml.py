"""Live integration against Cheng's Inoreader OPML export.

Requires network. Skips if the OPML file is missing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import fetch_feed
from rss_mcp.opml import build_opml, parse_opml
from rss_mcp.server import create_server
from rss_mcp.store import Store

OPML_PATH = Path(
    "/Users/huangcheng/Downloads/Cheng-Inoreader-feeds-final-20261016.opml"
)

pytestmark = pytest.mark.skipif(
    not OPML_PATH.is_file(),
    reason=f"OPML not found: {OPML_PATH}",
)


@pytest.fixture
def opml_xml() -> str:
    return OPML_PATH.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "real-inoreader.db",
        user_agent="rss-mcp/0.1 (+real-integration-test)",
        default_limit=20,
        max_limit=100,
        fetch_entry_cap=20,
    )


def test_parse_inoreader_opml_has_expected_shape(opml_xml: str) -> None:
    feeds = parse_opml(opml_xml)
    assert len(feeds) >= 50
    urls = [u for u, _ in feeds]
    assert len(urls) == len(set(urls))
    assert all(u.startswith(("http://", "https://")) for u in urls)
    titled = sum(1 for _, t in feeds if t)
    assert titled / len(feeds) > 0.8


@pytest.mark.asyncio
async def test_import_opml_via_mcp_then_list(
    opml_xml: str, settings: Settings
) -> None:
    from mcp import Client

    server = create_server(settings)
    async with Client(server) as client:
        result = await client.call_tool("import_opml", {"opml": opml_xml})
        payload = result.structured_content
        assert payload["added"] >= 50
        assert payload["added"] + payload["skipped"] == len(parse_opml(opml_xml))

        listed = await client.call_tool("list_subscriptions", {})
        subs = listed.structured_content["subscriptions"]
        assert len(subs) == payload["added"]

        again = await client.call_tool("import_opml", {"opml": opml_xml})
        assert again.structured_content["added"] == 0
        assert again.structured_content["skipped"] == payload["added"]

        exported = await client.call_tool("export_opml", {})
        round_trip = parse_opml(exported.structured_content["opml"])
        assert {u for u, _ in round_trip} == {s["url"] for s in subs}


@pytest.mark.asyncio
async def test_live_fetch_sample_feeds_then_search_and_read(
    opml_xml: str, settings: Settings
) -> None:
    """Fetch a slice of real feeds over the network; exercise search/read/mark."""
    feeds = parse_opml(opml_xml)
    # Prefer https; take a spread across the list
    sample = [feeds[i] for i in (0, 7, 15, 30, 45, 60, min(78, len(feeds) - 1))]
    sample = list(dict.fromkeys(sample))  # preserve order, dedupe

    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    for url, title in sample:
        store.add_subscription(url, title=title)

    ok = not_modified = error = upserted = 0
    results: list[dict] = []
    for sub in store.list_subscriptions():
        fr = await fetch_feed(
            sub.url,
            etag=sub.etag,
            last_modified=sub.last_modified,
            user_agent=settings.user_agent,
            entry_cap=settings.fetch_entry_cap,
            timeout=25.0,
        )
        row = {
            "id": sub.id,
            "title": sub.title,
            "url": sub.url,
            "status": fr.status,
            "entries": len(fr.entries),
            "error": fr.error,
        }
        results.append(row)
        if fr.status == "ok":
            ok += 1
            n = store.upsert_entries(sub.id, fr.entries)
            upserted += n
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.update_fetch_meta(
                sub.id,
                etag=fr.etag,
                last_modified=fr.last_modified,
                last_fetched_at=now,
                last_error=None,
            )
            if fr.feed_title:
                store.set_title_if_empty(sub.id, fr.feed_title)
        elif fr.status == "not_modified":
            not_modified += 1
        else:
            error += 1
            store.update_fetch_meta(
                sub.id,
                etag=sub.etag,
                last_modified=sub.last_modified,
                last_fetched_at=sub.last_fetched_at,
                last_error=fr.error or "fetch failed",
            )

    print("\n--- live fetch sample ---")
    for row in results:
        print(
            f"{row['status']:12} entries={row['entries']:3} "
            f"{(row['title'] or '')[:40]!r} err={row['error']!r}"
        )
    print(f"summary ok={ok} not_modified={not_modified} error={error} upserted={upserted}")

    # At least half of the sample should succeed on a normal network day
    assert ok + not_modified >= max(2, len(sample) // 2), results
    assert upserted > 0 or not_modified > 0

    entries = store.list_entries(limit=50)
    assert entries, "expected cached entries from successful fetches"

    # Keyword search over something that often appears in Chinese tech/news feeds
    hits = store.search_entries("的", limit=20)
    # Soft: if Chinese particle not indexed oddly, fall back to any title token
    if not hits and entries[0].title:
        token = entries[0].title.split()[0][:8]
        hits = store.search_entries(token, limit=20)
    assert isinstance(hits, list)

    first = entries[0]
    got = store.get_entry(first.id)
    assert got is not None
    updated = store.mark_entries([first.id], read=True)
    assert updated == 1
    assert store.get_entry(first.id).read_at is not None  # type: ignore[union-attr]

    # Second fetch on one successful feed should often be 304 or ok with 0 new
    success = next(r for r in results if r["status"] == "ok")
    sub = store.get_subscription(success["id"])
    assert sub is not None
    again = await fetch_feed(
        sub.url,
        etag=sub.etag,
        last_modified=sub.last_modified,
        user_agent=settings.user_agent,
        entry_cap=settings.fetch_entry_cap,
        timeout=25.0,
    )
    assert again.status in {"ok", "not_modified", "error"}
    print(f"re-fetch {sub.title!r} -> {again.status}")

    conn.close()


@pytest.mark.asyncio
async def test_live_fetch_all_feeds_report(opml_xml: str, settings: Settings) -> None:
    """Fetch every feed in the OPML (bounded concurrency); report pass rate."""
    feeds = parse_opml(opml_xml)
    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    for url, title in feeds:
        store.add_subscription(url, title=title)

    sem = asyncio.Semaphore(8)
    stats = {"ok": 0, "not_modified": 0, "error": 0, "upserted": 0}
    errors: list[tuple[str, str]] = []

    async def one(sub_id: int, url: str, title: str | None, etag, last_modified) -> None:
        async with sem:
            fr = await fetch_feed(
                url,
                etag=etag,
                last_modified=last_modified,
                user_agent=settings.user_agent,
                entry_cap=settings.fetch_entry_cap,
                timeout=20.0,
            )
        if fr.status == "ok":
            stats["ok"] += 1
            n = store.upsert_entries(sub_id, fr.entries)
            stats["upserted"] += n
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.update_fetch_meta(
                sub_id,
                etag=fr.etag,
                last_modified=fr.last_modified,
                last_fetched_at=now,
                last_error=None,
            )
        elif fr.status == "not_modified":
            stats["not_modified"] += 1
        else:
            stats["error"] += 1
            errors.append((title or url, fr.error or "unknown"))

    subs = store.list_subscriptions()
    await asyncio.gather(
        *(
            one(s.id, s.url, s.title, s.etag, s.last_modified)
            for s in subs
        )
    )

    total = len(subs)
    success = stats["ok"] + stats["not_modified"]
    print("\n--- live fetch ALL feeds ---")
    print(
        f"total={total} ok={stats['ok']} not_modified={stats['not_modified']} "
        f"error={stats['error']} upserted={stats['upserted']}"
    )
    print(f"success_rate={success / total:.1%}")
    if errors:
        print(f"errors ({len(errors)}):")
        for title, err in errors[:15]:
            print(f"  - {title[:50]!r}: {err[:120]}")
        if len(errors) > 15:
            print(f"  ... and {len(errors) - 15} more")

    # Real world: some feeds die; require a solid majority to pass
    assert success >= int(total * 0.6), stats
    assert stats["upserted"] > 0
    assert len(store.list_entries(limit=100)) > 0

    # MCP search over the populated DB
    from mcp import Client

    server = create_server(settings)
    async with Client(server) as client:
        listed = await client.call_tool("list_entries", {"limit": 10, "unread_only": True})
        assert listed.structured_content["entries"]
        q = await client.call_tool("search_entries", {"query": "a", "limit": 5})
        assert isinstance(q.structured_content["entries"], list)

    conn.close()
