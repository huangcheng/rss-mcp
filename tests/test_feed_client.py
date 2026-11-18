from pathlib import Path

import httpx
import pytest

from rss_mcp.feed_client import fetch_feed
from rss_mcp.models import EntryInput

FIXTURES = Path(__file__).parent / "fixtures"
RSS = (FIXTURES / "sample_rss.xml").read_bytes()
ATOM = (FIXTURES / "sample_atom.xml").read_bytes()


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rss_mcp.feed_client.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_fetch_feed_parses_rss_caps_entries_and_sends_validators(monkeypatch):
    # Drops the cap, skips If-None-Match, or maps summary into content.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers["user-agent"]
        seen["if_none_match"] = request.headers["if-none-match"]
        seen["if_modified_since"] = request.headers["if-modified-since"]
        seen["read_timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(
            200,
            headers={
                "ETag": '"rss-etag"',
                "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            content=RSS,
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag='"old"',
        last_modified="Tue, 31 Dec 2025 00:00:00 GMT",
        user_agent="rss-mcp/test",
        entry_cap=2,
    )

    assert seen == {
        "user_agent": "rss-mcp/test",
        "if_none_match": '"old"',
        "if_modified_since": "Tue, 31 Dec 2025 00:00:00 GMT",
        "read_timeout": 30.0,
    }
    assert result.status == "ok"
    assert result.error is None
    assert result.feed_title == "Sample RSS"
    assert result.etag == '"rss-etag"'
    assert result.last_modified == "Wed, 01 Jan 2026 00:00:00 GMT"
    assert result.entries == [
        EntryInput(
            guid="rss-1",
            url="https://example.com/1",
            title="First post",
            author="Ada",
            published_at="2026-01-01T00:00:00Z",
            summary="Short summary",
            content="<p>Full content</p>",
        ),
        EntryInput(
            guid="rss-2",
            url="https://example.com/2",
            title="Second post",
            author=None,
            published_at="2026-01-02T12:30:00Z",
            summary="Only summary",
            content=None,
        ),
    ]


@pytest.mark.asyncio
async def test_fetch_feed_parses_atom_without_conditional_headers(monkeypatch):
    # Prefers <updated> over <published>, or sends validators that were not provided.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_none_match"] = request.headers.get("if-none-match")
        seen["if_modified_since"] = request.headers.get("if-modified-since")
        return httpx.Response(200, content=ATOM)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/atom.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=10,
    )

    assert seen == {"if_none_match": None, "if_modified_since": None}
    assert result.status == "ok"
    assert result.error is None
    assert result.feed_title == "Sample Atom"
    assert result.etag is None
    assert result.last_modified is None
    assert result.entries == [
        EntryInput(
            guid="urn:atom:1",
            url="https://example.com/atom-1",
            title="Atom post",
            author="Grace",
            published_at="2026-02-03T04:05:06Z",
            summary="Atom summary",
            content="<p>Atom body</p>",
        ),
        EntryInput(
            guid="urn:atom:2",
            url="https://example.com/atom-2",
            title="Updated only",
            author=None,
            published_at="2026-03-04T05:06:07Z",
            summary="Later summary",
            content=None,
        ),
    ]


@pytest.mark.asyncio
async def test_fetch_feed_uses_explicit_timeout(monkeypatch):
    # Leaves the httpx default (5s) in place instead of the caller's timeout.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["read_timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(304)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=1,
        timeout=9.0,
    )

    assert seen["read_timeout"] == 9.0
    assert result.status == "not_modified"


@pytest.mark.asyncio
async def test_fetch_feed_304_is_not_modified_with_empty_entries(monkeypatch):
    # Treats 304 as a successful parse or keeps the previous body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            304,
            headers={
                "ETag": '"fresh"',
                "Last-Modified": "Thu, 02 Jan 2026 00:00:00 GMT",
            },
            content=RSS,
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag='"stale"',
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "not_modified"
    assert result.entries == []
    assert result.error is None
    assert result.feed_title is None
    assert result.etag == '"fresh"'
    assert result.last_modified == "Thu, 02 Jan 2026 00:00:00 GMT"


@pytest.mark.asyncio
async def test_fetch_feed_304_keeps_request_validators_when_response_omits_them(monkeypatch):
    # Returns None validators and would wipe the stored ETag on the next save.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag='"keep"',
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "not_modified"
    assert result.entries == []
    assert result.etag == '"keep"'
    assert result.last_modified == "Wed, 01 Jan 2026 00:00:00 GMT"


@pytest.mark.asyncio
async def test_fetch_feed_http_500_is_error(monkeypatch):
    # Raises, or parses the error page as a feed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<rss><channel><title>nope</title></channel></rss>")

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag='"keep"',
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "error"
    assert result.entries == []
    assert result.feed_title is None
    assert result.etag is None
    assert result.last_modified is None
    assert result.error is not None and "500" in result.error


@pytest.mark.asyncio
async def test_fetch_feed_garbage_body_is_error(monkeypatch):
    # Returns ok with no entries for a body that is not a feed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not a feed")

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "error"
    assert result.entries == []
    assert result.feed_title is None
    assert result.error


@pytest.mark.asyncio
async def test_fetch_feed_network_error_is_result_not_exception(monkeypatch):
    # Lets ConnectError escape the client.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "error"
    assert result.entries == []
    assert result.error is not None and "connection refused" in result.error


@pytest.mark.asyncio
async def test_fetch_feed_follows_redirect_to_the_feed(monkeypatch):
    # Stops on the first 302.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://example.com/feed.xml"})
        return httpx.Response(
            200,
            headers={"ETag": '"atom"'},
            content=ATOM,
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/start",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=10,
    )

    assert result.status == "ok"
    assert result.etag == '"atom"'
    assert [entry.guid for entry in result.entries] == ["urn:atom:1", "urn:atom:2"]


@pytest.mark.asyncio
async def test_fetch_feed_empty_channel_is_ok(monkeypatch):
    # Treats a feed with no items as a parse failure.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty Feed</title></channel></rss>',
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/empty.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "ok"
    assert result.error is None
    assert result.feed_title == "Empty Feed"
    assert result.entries == []


@pytest.mark.asyncio
async def test_fetch_feed_keeps_entries_when_parser_reports_bozo(monkeypatch):
    # Discards a usable feed because the XML is not strict.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b"<rss version='2.0'><channel><title>Loose</title>"
                b"<item><title>A & B</title><guid>g1</guid>"
                b"<link>https://example.com/a</link></item></channel></rss>"
            ),
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/loose.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "ok"
    assert result.error is None
    assert result.feed_title == "Loose"
    assert result.entries == [
        EntryInput(
            guid="g1",
            url="https://example.com/a",
            title="A & B",
            author=None,
            published_at=None,
            summary=None,
            content=None,
        )
    ]


@pytest.mark.asyncio
async def test_fetch_feed_maps_blank_fields_to_none(monkeypatch):
    # Stores "" and a later upsert deletes a cached article body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel><title>   </title>
    <item>
      <title>   </title>
      <link>   </link>
      <guid>   </guid>
      <description>   </description>
      <content:encoded><![CDATA[   ]]></content:encoded>
    </item>
  </channel>
</rss>""",
        )

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/blank.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=5,
    )

    assert result.status == "ok"
    assert result.feed_title is None
    assert result.entries == [
        EntryInput(
            guid=None,
            url=None,
            title=None,
            author=None,
            published_at=None,
            summary=None,
            content=None,
        )
    ]


@pytest.mark.asyncio
async def test_fetch_feed_zero_and_negative_cap_return_no_entries(monkeypatch):
    # A negative cap uses Python's tail slice and still returns items.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS)

    _install(monkeypatch, handler)
    for cap in (0, -1):
        result = await fetch_feed(
            "https://example.com/rss.xml",
            etag=None,
            last_modified=None,
            user_agent="rss-mcp/test",
            entry_cap=cap,
        )
        assert result.status == "ok"
        assert result.feed_title == "Sample RSS"
        assert result.entries == []


@pytest.mark.asyncio
async def test_fetch_feed_rejects_body_over_the_size_cap(monkeypatch):
    # Parses a body larger than the configured cap.
    monkeypatch.setattr("rss_mcp.feed_client.MAX_FEED_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag=None,
        last_modified=None,
        user_agent="rss-mcp/test",
        entry_cap=50,
    )

    assert result.status == "error"
    assert result.entries == []
    assert result.feed_title is None
    assert result.error
