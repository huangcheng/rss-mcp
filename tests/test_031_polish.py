"""0.3.1 polish: digest per-feed cap, plain-text snippets, filter parity,
failing-feed triage."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.feed_client import FetchResult
from rss_mcp.models import EntryInput
from rss_mcp.store import Store


def _seed(settings: Settings) -> Store:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    return Store(conn)


def _entries(prefix: str, n: int) -> list[EntryInput]:
    return [
        EntryInput(
            guid=f"{prefix}-{i}",
            url=f"https://x.example/{prefix}/{i}",
            title=f"{prefix} story {i}",
            published_at=f"2026-11-{19 - i:02d}T00:00:00Z" if i < 19 else None,
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_digest_caps_per_feed_globally_and_by_tag(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    heavy = store.add_subscription("https://heavy.example/f", tags=["tech"])
    light = store.add_subscription("https://light.example/f", tags=["tech"])
    store.upsert_entries(heavy.id, _entries("heavy", 10))
    store.upsert_entries(light.id, _entries("light", 2))

    async with Client(create_server(server_settings)) as client:
        default = await client.call_tool("get_digest", {"max_entries": 20})
        capped = await client.call_tool("get_digest", {"max_entries": 20, "per_feed": 1})
        tagged = await client.call_tool("get_digest", {"tag": "tech", "max_entries": 20})

    entries = default.structured_content["entries"]
    counts: dict[int, int] = {}
    for e in entries:
        counts[e["subscription_id"]] = counts.get(e["subscription_id"], 0) + 1
    assert counts[heavy.id] == 3
    assert counts[light.id] == 2
    assert len(entries) == 5

    assert len(capped.structured_content["entries"]) == 2
    tagged_entries = tagged.structured_content["entries"]
    assert sum(1 for e in tagged_entries if e["subscription_id"] == heavy.id) == 3


@pytest.mark.asyncio
async def test_search_snippet_is_plain_text(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    sub = store.add_subscription("https://html.example/f")
    store.upsert_entries(
        sub.id,
        [EntryInput(guid="h1", title="Zephyr", content="<p>The <b>zephyr</b> winds &amp; rains</p>")],
    )
    async with Client(create_server(server_settings)) as client:
        found = await client.call_tool("search_entries", {"query": "zephyr"})

    snippet = found.structured_content["entries"][0]["snippet"]
    assert snippet is not None
    assert "<" not in snippet and ">" not in snippet
    assert "zephyr" in snippet.lower()
    assert "&amp;" not in snippet


@pytest.mark.asyncio
async def test_list_entries_filters_by_tag_and_starred(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    tech = store.add_subscription("https://tech.example/f", tags=["tech"])
    news = store.add_subscription("https://news.example/f", tags=["news"])
    store.upsert_entries(tech.id, [EntryInput(guid="t1", title="Tech one")])
    store.upsert_entries(news.id, [EntryInput(guid="n1", title="News one")])

    async with Client(create_server(server_settings)) as client:
        tech_entries = await client.call_tool("list_entries", {"tag": "tech"})
        assert [e["guid"] for e in tech_entries.structured_content["entries"]] == ["t1"]

        tech_id = tech_entries.structured_content["entries"][0]["id"]
        await client.call_tool("star_entry", {"entry_ids": [tech_id], "starred": True})
        starred = await client.call_tool("list_entries", {"starred": True})
        assert [e["guid"] for e in starred.structured_content["entries"]] == ["t1"]
        unstarred = await client.call_tool("list_entries", {"starred": False})
        assert [e["guid"] for e in unstarred.structured_content["entries"]] == ["n1"]


@pytest.mark.asyncio
async def test_search_filters_by_tag_and_since(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    tech = store.add_subscription("https://tech.example/f", tags=["tech"])
    news = store.add_subscription("https://news.example/f", tags=["news"])
    store.upsert_entries(
        tech.id,
        [
            EntryInput(guid="t-old", title="Zephyr old", content="zephyr", published_at="2026-01-01T00:00:00Z"),
            EntryInput(guid="t-new", title="Zephyr new", content="zephyr", published_at="2026-11-01T00:00:00Z"),
        ],
    )
    store.upsert_entries(
        news.id,
        [EntryInput(guid="n1", title="Zephyr news", content="zephyr", published_at="2026-11-01T00:00:00Z")],
    )

    async with Client(create_server(server_settings)) as client:
        scoped = await client.call_tool("search_entries", {"query": "zephyr", "tag": "tech"})
        assert {e["guid"] for e in scoped.structured_content["entries"]} == {"t-old", "t-new"}
        recent = await client.call_tool(
            "search_entries", {"query": "zephyr", "since": "2026-06-01T00:00:00Z"}
        )
        assert {e["guid"] for e in recent.structured_content["entries"]} == {"t-new", "n1"}


@pytest.mark.asyncio
async def test_failing_only_triage_with_hints(server_settings: Settings, monkeypatch) -> None:
    from rss_mcp import server

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        if "blocked" in url:
            return FetchResult(status="error", entries=[], etag=None, last_modified=None, feed_title=None, error="HTTP 403")
        if "gone" in url:
            return FetchResult(status="error", entries=[], etag=None, last_modified=None, feed_title=None, error="HTTP 404")
        return FetchResult(status="ok", entries=[], etag=None, last_modified=None, feed_title=None, error=None)

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        blocked = await client.call_tool("add_subscription", {"url": "https://blocked.example/f"})
        gone = await client.call_tool("add_subscription", {"url": "https://gone.example/f"})
        healthy = await client.call_tool("add_subscription", {"url": "https://ok.example/f"})
        for sub in (blocked, gone, healthy):
            await client.call_tool("fetch_feed", {"subscription_id": sub.structured_content["id"]})
        failing = await client.call_tool("list_subscriptions", {"failing_only": True})

    rows = failing.structured_content["subscriptions"]
    assert {r["url"] for r in rows} == {"https://blocked.example/f", "https://gone.example/f"}
    by_url = {r["url"]: r for r in rows}
    assert "user_agent" in by_url["https://blocked.example/f"]["remediation"]
    assert "remove_subscription" in by_url["https://gone.example/f"]["remediation"]
    healthy_rows = [r for r in (await _healthy_rows(server_settings)) if r["url"] == "https://ok.example/f"]
    assert len(healthy_rows) == 1
    assert "remediation" not in healthy_rows[0]


async def _healthy_rows(settings: Settings) -> list[dict]:
    from rss_mcp.server import create_server

    async with Client(create_server(settings)) as client:
        result = await client.call_tool("list_subscriptions", {})
    return result.structured_content["subscriptions"]


@pytest.mark.asyncio
async def test_disabled_feed_hint_mentions_reenable(tmp_path: Path, monkeypatch) -> None:
    from rss_mcp import server

    settings = Settings(db_path=tmp_path / "disable.db", user_agent="rss-mcp/test", disable_after_failures=1)

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(status="error", entries=[], etag=None, last_modified=None, feed_title=None, error="HTTP 502")

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(settings)) as client:
        sub = await client.call_tool("add_subscription", {"url": "https://flaky.example/f"})
        await client.call_tool("fetch_feed", {"subscription_id": sub.structured_content["id"]})
        failing = await client.call_tool("list_subscriptions", {"failing_only": True})

    row = failing.structured_content["subscriptions"][0]
    assert row["disabled"] is True
    assert "re-enables" in row["remediation"]


@pytest.mark.asyncio
async def test_snippet_preserves_escaped_literals_and_drops_scripts(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    sub = store.add_subscription("https://code.example/f")
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="c1", title="Div talk", content="use &lt;div&gt; for blocks"),
            EntryInput(guid="c2", title="Script talk", content="<script>alert(1)</script><p>zephyr text</p>"),
            EntryInput(guid="c3", title="Math talk", content="compare 5 < 6 and 7 > 3 then zephyr"),
        ],
    )
    async with Client(create_server(server_settings)) as client:
        div_hits = await client.call_tool("search_entries", {"query": "div"})
        script_hits = await client.call_tool("search_entries", {"query": "zephyr"})

    div_snippet = div_hits.structured_content["entries"][0]["snippet"]
    assert "div" in div_snippet.lower()
    assert "<" not in div_snippet and ">" not in div_snippet
    snippets = " ".join(e["snippet"] or "" for e in script_hits.structured_content["entries"])
    assert "alert" not in snippets
    assert "6 and 7" in snippets


def test_clean_snippet_edge_cases() -> None:
    from rss_mcp.server import _clean_snippet

    assert _clean_snippet("<p>A</p>\n\n<p>B</p>") == "A B"
    assert _clean_snippet("<br/>") is None
    assert _clean_snippet(None) is None
    assert _clean_snippet("   ") is None
    unclosed = _clean_snippet('<img src=x onerror="alert(1)"')
    assert unclosed is None or ("<" not in unclosed and ">" not in unclosed)
    fragment = _clean_snippet("b>[zephyr] winds blow</p>")
    assert fragment is not None and "<" not in fragment and ">" not in fragment
    assert "zephyr" in fragment


def test_remediation_classification() -> None:
    from rss_mcp.models import Subscription
    from rss_mcp.server import _remediation_hint

    def sub_with(error: str | None, disabled: bool = False) -> Subscription:
        return Subscription(
            id=1, url="https://x.example/f", title=None, tags=[], etag=None,
            last_modified=None, last_fetched_at=None, last_error=error,
            created_at="2026-01-01T00:00:00Z", disabled=disabled,
        )

    parse_error = sub_with("unable to parse feed: not well-formed (invalid token) at line 404, column 12")
    hint = _remediation_hint(parse_error)
    assert "transient" in hint and "gone" not in hint

    ssl_error = sub_with("SSL: CERTIFICATE_VERIFY_FAILED (_ssl.c:1401)")
    assert "TLS" in _remediation_hint(ssl_error)

    assert "user_agent" in _remediation_hint(sub_with("HTTP 401"))
    assert "user_agent" in _remediation_hint(sub_with("HTTP 403"))
    gone = _remediation_hint(sub_with("HTTP 404"))
    assert "remove_subscription" in gone and "add_subscription" in gone
    assert "remove_subscription" in _remediation_hint(sub_with("HTTP 410"))
    assert "transient" in _remediation_hint(sub_with("HTTP 502"))
    assert "transient" in _remediation_hint(sub_with("connection timeout"))
    assert _remediation_hint(sub_with(None)) is None


@pytest.mark.asyncio
async def test_manually_disabled_feed_appears_in_triage(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    sub = store.add_subscription("https://paused.example/f")
    store.update_subscription(sub.id, disabled=True)

    async with Client(create_server(server_settings)) as client:
        failing = await client.call_tool("list_subscriptions", {"failing_only": True})

    rows = failing.structured_content["subscriptions"]
    assert [r["id"] for r in rows] == [sub.id]
    assert rows[0]["last_error"] is None
    assert "re-enables" in rows[0]["remediation"]


@pytest.mark.asyncio
async def test_digest_copy_stands_in_for_deep_keeper(server_settings: Settings) -> None:
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    heavy = store.add_subscription("https://heavy.example/f")
    light = store.add_subscription("https://light.example/f")
    shared_url = "https://x.example/shared"
    store.upsert_entries(
        heavy.id,
        [EntryInput(guid="keeper", url=shared_url, title="Shared story", published_at="2026-11-01T00:00:00Z")]
        + [
            EntryInput(guid=f"new-{i}", url=f"https://x.example/new/{i}", title=f"New {i}",
                       published_at=f"2026-11-{15 + i:02d}T00:00:00Z")
            for i in range(3)
        ],
    )
    store.upsert_entries(
        light.id,
        [EntryInput(guid="copy", url=shared_url, title="Shared story", published_at="2026-11-01T00:00:00Z")],
    )

    async with Client(create_server(server_settings)) as client:
        digest = await client.call_tool("get_digest", {"max_entries": 20})

    entries = digest.structured_content["entries"]
    assert any(e["url"] == shared_url for e in entries), "deep keeper's article vanished"
    assert sum(1 for e in entries if e["url"] == shared_url) == 1


@pytest.mark.asyncio
async def test_digest_copies_only_feed_still_contributes(server_settings: Settings) -> None:
    # A feed whose only unread entries are duplicate copies has unread_count 0
    # but must still be queried so its copies can stand in for deep keepers.
    from rss_mcp.server import create_server

    store = _seed(server_settings)
    heavy = store.add_subscription("https://heavy.example/f")
    light = store.add_subscription("https://light.example/f")
    shared_url = "https://x.example/shared2"
    store.upsert_entries(
        heavy.id,
        [EntryInput(guid="keeper2", url=shared_url, title="Shared", published_at="2026-11-01T00:00:00Z")]
        + [
            EntryInput(guid=f"n2-{i}", url=f"https://x.example/n2/{i}", title=f"N {i}",
                       published_at=f"2026-11-{15 + i:02d}T00:00:00Z")
            for i in range(3)
        ],
    )
    store.upsert_entries(
        light.id,
        [EntryInput(guid="copy2", url=shared_url, title="Shared", published_at="2026-11-01T00:00:00Z")],
    )
    assert store.get_subscription(light.id).unread_count == 0

    async with Client(create_server(server_settings)) as client:
        digest = await client.call_tool("get_digest", {"max_entries": 20})

    entries = digest.structured_content["entries"]
    assert any(e["url"] == shared_url for e in entries)