from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import FetchResult
from rss_mcp.models import EntryInput, Subscription
from rss_mcp.store import Store

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

def _open_store(settings: Settings) -> tuple[sqlite3.Connection, Store]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _load_subscription(settings: Settings, subscription_id: int) -> Subscription:
    conn = connect(settings.db_path)
    try:
        found = Store(conn).get_subscription(subscription_id)
    finally:
        conn.close()
    assert found is not None
    return found


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def _call(client: Client, name: str, arguments: dict | None = None) -> CallToolResult:
    return await client.call_tool(name, arguments or {})


async def _ok(client: Client, name: str, arguments: dict | None = None) -> dict:
    result = await _call(client, name, arguments)
    assert result.is_error is False, _text(result)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.mark.asyncio
async def test_tools_list_is_exactly_the_ten_tools_in_table_order(server_settings: Settings) -> None:
    # Drops a tool, renames one, or lists them in a different order.
    from rss_mcp.server import create_server

    server = create_server(server_settings)
    assert server.name == "rss-mcp"
    assert server.version == "0.1.1"
    async with Client(server) as client:
        listed = await client.list_tools()
        caps = client.server_capabilities
        info = client.server_info
    assert [tool.name for tool in listed.tools] == TOOL_NAMES
    assert caps.tools is not None
    assert caps.resources is None
    assert caps.prompts is None
    assert info is not None
    assert info.version == "0.1.1"
    # subscriptions=False: hosts should not expect a live tools listChanged stream
    assert "subscriptions/listen" not in server._lowlevel_server._request_handlers


@pytest.mark.asyncio
async def test_add_subscription_round_trips_through_list(server_settings: Settings) -> None:
    # Omits tags, unread_count, or fails to create the parent directory of the db.
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        added = await _ok(
            client,
            "add_subscription",
            {
                "url": "  https://example.com/feed.xml  ",
                "title": "Example",
                "tags": ["news", "tech"],
            },
        )
        listed = await _ok(client, "list_subscriptions")

    assert server_settings.db_path.is_file()
    assert added["url"] == "https://example.com/feed.xml"
    assert added["title"] == "Example"
    assert added["tags"] == ["news", "tech"]
    assert added["unread_count"] == 0
    assert added["last_error"] is None
    assert listed["subscriptions"] == [added]


@pytest.mark.asyncio
async def test_remove_subscription_reports_whether_the_row_existed(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        added = await _ok(client, "add_subscription", {"url": "https://example.com/feed.xml"})
        removed = await _ok(client, "remove_subscription", {"subscription_id": added["id"]})
        missing = await _ok(client, "remove_subscription", {"subscription_id": added["id"]})
        listed = await _ok(client, "list_subscriptions")

    assert removed == {"ok": True}
    assert missing == {"ok": False}
    assert listed["subscriptions"] == []


@pytest.mark.asyncio
async def test_search_entries_finds_text_upserted_in_the_store(server_settings: Settings) -> None:
    # Searches the wrong subscription, ignores unread_only, or skips the FTS index.
    from rss_mcp.server import create_server

    conn, store = _open_store(server_settings)
    first = store.add_subscription("https://example.com/a.xml")
    second = store.add_subscription("https://example.com/b.xml")
    store.upsert_entries(
        first.id,
        [
            EntryInput(guid="a1", title="Orbital pass", summary="evening", content="alpha"),
            EntryInput(guid="a2", title="Orbital dock", summary="morning", content="beta"),
        ],
    )
    store.upsert_entries(
        second.id,
        [EntryInput(guid="b1", title="Orbital rumor", summary="nope", content="gamma")],
    )
    read_id = next(
        entry.id
        for entry in store.list_entries(subscription_id=first.id, limit=10)
        if entry.guid == "a1"
    )
    store.mark_entries([read_id], read=True)
    conn.close()

    narrow = Settings(
        db_path=server_settings.db_path,
        user_agent=server_settings.user_agent,
        default_limit=1,
        max_limit=1,
    )
    async with Client(create_server(narrow)) as client:
        page = await _ok(client, "search_entries", {"query": "Orbital"})
        capped = await _ok(client, "search_entries", {"query": "Orbital", "limit": 50})
        filtered = await _ok(
            client,
            "search_entries",
            {"query": "Orbital", "subscription_id": first.id, "unread_only": True, "limit": 10},
        )
        empty = await _ok(client, "search_entries", {"query": "   "})

    assert len(page["entries"]) == 1
    assert len(capped["entries"]) == 1
    assert [entry["guid"] for entry in filtered["entries"]] == ["a2"]
    assert empty["entries"] == []
    assert all(entry["content"] is None for entry in page["entries"])
    assert all(entry["content"] is None for entry in filtered["entries"])


@pytest.mark.asyncio
async def test_fetch_feed_ok_upserts_clears_error_and_fills_blank_title(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Writes the error result's empty validators, leaves last_error set, or keeps a blank title.
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml", title="   ")
    store.update_fetch_meta(
        sub.id,
        etag="old-etag",
        last_modified="old-modified",
        last_fetched_at="2000-01-01T00:00:00Z",
        last_error="previous",
    )
    conn.close()
    seen: dict[str, object] = {}

    async def fake_fetch(url: str, **kwargs: object) -> FetchResult:
        seen["url"] = url
        seen["kwargs"] = kwargs
        return FetchResult(
            status="ok",
            entries=[
                EntryInput(
                    guid="g1",
                    url="https://example.com/1",
                    title="First",
                    published_at="2026-03-01T00:00:00Z",
                    summary="one",
                    content="body",
                )
            ],
            etag="new-etag",
            last_modified="new-modified",
            feed_title="  From Feed  ",
            error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await _ok(client, "fetch_feed", {"subscription_id": sub.id})

    stored = _load_subscription(server_settings, sub.id)
    assert seen["url"] == "https://example.com/feed.xml"
    assert seen["kwargs"] == {
        "etag": "old-etag",
        "last_modified": "old-modified",
        "user_agent": "rss-mcp/test",
        "entry_cap": 50,
        "timeout": 30.0,
    }
    assert summary == {
        "ok": 1,
        "not_modified": 0,
        "error": 0,
        "upserted": 1,
        "results": [
            {"subscription_id": sub.id, "status": "ok", "upserted": 1, "error": None},
        ],
    }
    assert stored.etag == "new-etag"
    assert stored.last_modified == "new-modified"
    assert stored.last_error is None
    assert stored.last_fetched_at != "2000-01-01T00:00:00Z"
    assert stored.title == "From Feed"
    assert stored.unread_count == 1


@pytest.mark.asyncio
async def test_fetch_feed_ok_keeps_an_existing_title(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml", title="Kept")
    conn.close()

    async def fake_fetch(url: str, **kwargs: object) -> FetchResult:
        return FetchResult(
            status="ok",
            entries=[],
            etag=None,
            last_modified=None,
            feed_title="From Feed",
            error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        await _ok(client, "fetch_feed", {"subscription_id": sub.id})

    assert _load_subscription(server_settings, sub.id).title == "Kept"


@pytest.mark.asyncio
async def test_fetch_feed_error_keeps_validators_and_records_last_error(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Overwrites etag/last_modified with the error result's Nones, or refreshes last_fetched_at.
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml", title="Kept")
    store.update_fetch_meta(
        sub.id,
        etag="keep-etag",
        last_modified="keep-modified",
        last_fetched_at="2000-01-01T00:00:00Z",
        last_error=None,
    )
    store.upsert_entries(sub.id, [EntryInput(guid="g1", title="Stay")])
    conn.close()

    async def fake_fetch(url: str, **kwargs: object) -> FetchResult:
        return FetchResult(
            status="error",
            entries=[EntryInput(guid="should-not-land", title="Nope")],
            etag="bogus-etag",
            last_modified="bogus-modified",
            feed_title="Bogus",
            error="HTTP 500",
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await _ok(client, "fetch_feed", {"subscription_id": sub.id})
        listed = await _ok(client, "list_entries", {"subscription_id": sub.id})

    stored = _load_subscription(server_settings, sub.id)
    assert summary["ok"] == 0
    assert summary["error"] == 1
    assert summary["upserted"] == 0
    assert summary["results"] == [
        {"subscription_id": sub.id, "status": "error", "upserted": 0, "error": "HTTP 500"},
    ]
    assert stored.etag == "keep-etag"
    assert stored.last_modified == "keep-modified"
    assert stored.last_fetched_at == "2000-01-01T00:00:00Z"
    assert stored.last_error == "HTTP 500"
    assert stored.title == "Kept"
    assert [entry["guid"] for entry in listed["entries"]] == ["g1"]


@pytest.mark.asyncio
async def test_fetch_feed_not_modified_stores_result_validators_and_fetched_at(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    store.update_fetch_meta(
        sub.id,
        etag="old-etag",
        last_modified="old-modified",
        last_fetched_at="2000-01-01T00:00:00Z",
        last_error="stale",
    )
    conn.close()

    async def fake_fetch(url: str, **kwargs: object) -> FetchResult:
        return FetchResult(
            status="not_modified",
            entries=[EntryInput(guid="should-not-land", title="Nope")],
            etag="fresh-etag",
            last_modified="fresh-modified",
            feed_title="Ignored",
            error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await _ok(client, "fetch_feed", {"subscription_id": sub.id})
        listed = await _ok(client, "list_entries")

    stored = _load_subscription(server_settings, sub.id)
    assert summary["not_modified"] == 1
    assert summary["upserted"] == 0
    assert stored.etag == "fresh-etag"
    assert stored.last_modified == "fresh-modified"
    assert stored.last_fetched_at != "2000-01-01T00:00:00Z"
    assert stored.last_error is None
    assert stored.title is None
    assert listed["entries"] == []


@pytest.mark.asyncio
async def test_fetch_feed_without_id_refreshes_every_subscription_and_unknown_id_errors(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    first = store.add_subscription("https://example.com/a.xml")
    second = store.add_subscription("https://example.com/b.xml")
    conn.close()
    fetched: list[str] = []

    async def fake_fetch(url: str, **kwargs: object) -> FetchResult:
        fetched.append(url)
        if url.endswith("a.xml"):
            return FetchResult(
                status="ok",
                entries=[EntryInput(guid="g", title="A")],
                etag="e",
                last_modified="m",
                feed_title="A feed",
                error=None,
            )
        return FetchResult(
            status="error",
            entries=[],
            etag="e",
            last_modified="m",
            feed_title="B feed",
            error="down",
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        summary = await _ok(client, "fetch_feed", {})
        missing = await _call(client, "fetch_feed", {"subscription_id": 999})

    assert fetched == ["https://example.com/a.xml", "https://example.com/b.xml"]
    assert summary["ok"] == 1
    assert summary["error"] == 1
    assert summary["upserted"] == 1
    assert [row["subscription_id"] for row in summary["results"]] == [first.id, second.id]
    assert missing.is_error is True
    assert "subscription not found" in _text(missing)
    assert _load_subscription(server_settings, second.id).etag is None
    assert _load_subscription(server_settings, second.id).last_error == "down"


@pytest.mark.asyncio
async def test_list_entries_clamps_limit_and_filters(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    other = store.add_subscription("https://example.com/other.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="old", title="Old", published_at="2026-01-01T00:00:00Z"),
            EntryInput(guid="new", title="New", published_at="2026-06-01T00:00:00Z"),
            EntryInput(guid="mid", title="Mid", published_at="2026-03-01T00:00:00Z"),
        ],
    )
    store.upsert_entries(
        other.id,
        [EntryInput(guid="else", title="Else", published_at="2026-06-02T00:00:00Z")],
    )
    new_id = next(entry.id for entry in store.list_entries(subscription_id=sub.id, limit=10) if entry.guid == "new")
    store.mark_entries([new_id], read=True)
    conn.close()

    narrow = Settings(
        db_path=server_settings.db_path,
        user_agent=server_settings.user_agent,
        default_limit=1,
        max_limit=2,
    )
    async with Client(create_server(narrow)) as client:
        defaulted = await _ok(client, "list_entries", {"subscription_id": sub.id})
        capped = await _ok(client, "list_entries", {"subscription_id": sub.id, "limit": 50})
        zero = await _ok(client, "list_entries", {"limit": 0})
        negative = await _ok(client, "list_entries", {"limit": -3})
        unread = await _ok(
            client,
            "list_entries",
            {"subscription_id": sub.id, "unread_only": True, "limit": 10},
        )
        since = await _ok(
            client,
            "list_entries",
            {"subscription_id": sub.id, "since": "2026-03-01T00:00:00Z", "limit": 10},
        )

    assert [entry["guid"] for entry in defaulted["entries"]] == ["new"]
    assert [entry["guid"] for entry in capped["entries"]] == ["new", "mid"]
    assert zero["entries"] == []
    assert negative["entries"] == []
    assert [entry["guid"] for entry in unread["entries"]] == ["mid", "old"]
    assert [entry["guid"] for entry in since["entries"]] == ["new", "mid"]


@pytest.mark.asyncio
async def test_read_entry_caches_full_text_only_when_fetch_returns_a_string(
    server_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Caches None, skips a successful string, or fetches when fetch_full is false.
    from rss_mcp import server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(
                guid="with-url",
                url="https://example.com/post",
                title="Post",
                content="feed body",
            ),
            EntryInput(guid="no-url", title="Bare", content="leave me"),
        ],
    )
    entries = {entry.guid: entry for entry in store.list_entries(subscription_id=sub.id, limit=10)}
    conn.close()
    calls: list[str] = []

    async def fake_article(url: str, **kwargs: object) -> str | None:
        calls.append(url)
        assert kwargs["user_agent"] == "rss-mcp/test"
        if len(calls) == 1:
            return None
        return "zephyr-unique-token in the article"

    monkeypatch.setattr(server, "fetch_article_text", fake_article)
    async with Client(server.create_server(server_settings)) as client:
        cached = await _ok(client, "read_entry", {"entry_id": entries["with-url"].id})
        missed = await _ok(
            client,
            "read_entry",
            {"entry_id": entries["with-url"].id, "fetch_full": True},
        )
        filled = await _ok(
            client,
            "read_entry",
            {"entry_id": entries["with-url"].id, "fetch_full": True},
        )
        again = await _ok(client, "read_entry", {"entry_id": entries["with-url"].id})
        bare = await _ok(
            client,
            "read_entry",
            {"entry_id": entries["no-url"].id, "fetch_full": True},
        )
        missing = await _call(client, "read_entry", {"entry_id": 999})
        found = await _ok(client, "search_entries", {"query": "zephyr-unique-token"})

    assert calls == ["https://example.com/post", "https://example.com/post"]
    assert cached["content"] == "feed body"
    assert missed["content"] == "feed body"
    assert filled["content"] == "zephyr-unique-token in the article"
    assert again["content"] == "zephyr-unique-token in the article"
    assert bare["content"] == "leave me"
    assert missing.is_error is True
    assert "entry not found" in _text(missing)
    assert [entry["id"] for entry in found["entries"]] == [entries["with-url"].id]


@pytest.mark.asyncio
async def test_mark_entry_sets_and_clears_read_state(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid="g1", title="One"), EntryInput(guid="g2", title="Two")],
    )
    ids = [entry.id for entry in store.list_entries(subscription_id=sub.id, limit=10)]
    conn.close()

    async with Client(create_server(server_settings)) as client:
        marked = await _ok(client, "mark_entry", {"entry_ids": [ids[0], ids[0], 999], "read": True})
        listed = await _ok(client, "list_subscriptions")
        cleared = await _ok(client, "mark_entry", {"entry_ids": ids, "read": False})
        after = await _ok(client, "list_subscriptions")

    assert marked == {"updated": 1}
    assert listed["subscriptions"][0]["unread_count"] == 1
    assert cleared == {"updated": 1}
    assert after["subscriptions"][0]["unread_count"] == 2


@pytest.mark.asyncio
async def test_import_opml_counts_new_and_existing_and_rejects_invalid_xml(
    server_settings: Settings,
) -> None:
    # Lets ParseError crash the tool, or counts an existing URL as added.
    from rss_mcp.server import create_server

    opml = """<?xml version="1.0"?>
    <opml version="2.0"><body>
      <outline text="News">
        <outline type="rss" xmlUrl="https://example.com/a.xml" title="A"/>
      </outline>
      <outline type="rss" xmlUrl="https://example.com/a.xml" title="Again"/>
      <outline type="rss" xmlUrl="https://example.com/b.xml" text="B"/>
    </body></opml>"""
    async with Client(create_server(server_settings)) as client:
        await _ok(
            client,
            "add_subscription",
            {"url": "https://example.com/a.xml", "title": "Original"},
        )
        imported = await _ok(client, "import_opml", {"opml": opml})
        listed = await _ok(client, "list_subscriptions")
        bad = await _call(client, "import_opml", {"opml": "<opml>"})
        still = await _ok(client, "list_subscriptions")

    by_url = {row["url"]: row for row in listed["subscriptions"]}
    assert imported == {"added": 1, "skipped": 2}
    assert by_url["https://example.com/a.xml"]["title"] == "Original"
    assert by_url["https://example.com/b.xml"]["title"] == "B"
    assert bad.is_error is True
    assert "invalid OPML" in _text(bad)
    assert [row["url"] for row in still["subscriptions"]] == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]


@pytest.mark.asyncio
async def test_export_opml_contains_subscribed_feed_urls(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    async with Client(create_server(server_settings)) as client:
        await _ok(client, "add_subscription", {"url": "https://example.com/a.xml", "title": "A"})
        await _ok(client, "add_subscription", {"url": "https://example.com/b.xml"})
        exported = await _ok(client, "export_opml")

    assert exported["opml"].startswith("<?xml")
    assert 'xmlUrl="https://example.com/a.xml"' in exported["opml"]
    assert 'title="A"' in exported["opml"]
    assert 'xmlUrl="https://example.com/b.xml"' in exported["opml"]


@pytest.mark.asyncio
async def test_create_server_without_settings_uses_rss_mcp_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ignores RSS_MCP_DB and writes the default home database instead.
    from rss_mcp.server import create_server

    db = tmp_path / "env" / "rss.db"
    monkeypatch.setenv("RSS_MCP_DB", str(db))
    monkeypatch.delenv("RSS_MCP_USER_AGENT", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    async with Client(create_server()) as client:
        added = await _ok(client, "add_subscription", {"url": "https://example.com/env.xml"})

    assert db.is_file()
    assert added["url"] == "https://example.com/env.xml"
    assert not (tmp_path / "home" / ".local" / "share" / "rss-mcp" / "rss.db").exists()


@pytest.mark.asyncio
async def test_list_and_search_omit_content_bodies_read_keeps_full(
    server_settings: Settings,
) -> None:
    from rss_mcp.server import create_server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(
                guid="fat",
                title="Fat article",
                summary="short",
                content="<p>huge body that must not list</p>",
            )
        ],
    )
    entry_id = store.list_entries(subscription_id=sub.id, limit=1)[0].id
    conn.close()

    async with Client(create_server(server_settings)) as client:
        listed = await _ok(client, "list_entries", {"subscription_id": sub.id})
        found = await _ok(client, "search_entries", {"query": "Fat"})
        read = await _ok(client, "read_entry", {"entry_id": entry_id})

    assert listed["entries"][0]["content"] is None
    assert listed["entries"][0]["summary"] == "short"
    assert found["entries"][0]["content"] is None
    assert read["content"] == "<p>huge body that must not list</p>"


@pytest.mark.asyncio
async def test_read_entry_mark_read_default_false_and_true(
    server_settings: Settings,
) -> None:
    from rss_mcp.server import create_server

    conn, store = _open_store(server_settings)
    sub = store.add_subscription("https://example.com/feed.xml")
    store.upsert_entries(sub.id, [EntryInput(guid="g1", title="One", content="body")])
    entry_id = store.list_entries(subscription_id=sub.id, limit=1)[0].id
    conn.close()

    async with Client(create_server(server_settings)) as client:
        plain = await _ok(client, "read_entry", {"entry_id": entry_id})
        assert plain["read_at"] is None
        marked = await _ok(client, "read_entry", {"entry_id": entry_id, "mark_read": True})
        assert marked["read_at"] is not None
        again = await _ok(client, "read_entry", {"entry_id": entry_id})
        assert again["read_at"] is not None


def test_main_runs_the_stdio_server(monkeypatch: pytest.MonkeyPatch) -> None:
    # Leaves the Task 1 NotImplementedError in place, or selects another transport.
    from rss_mcp.server import MCPServer, main

    seen: dict[str, object] = {}

    def fake_run(self: MCPServer, transport: str = "stdio", **kwargs: object) -> None:
        seen["name"] = self.name
        seen["transport"] = transport
        seen["kwargs"] = kwargs

    monkeypatch.setattr(MCPServer, "run", fake_run)
    from rss_mcp.__main__ import main as console_main

    console_main()
    assert console_main is main
    assert seen == {"name": "rss-mcp", "transport": "stdio", "kwargs": {}}
