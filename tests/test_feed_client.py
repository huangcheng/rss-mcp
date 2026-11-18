import os
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
            enclosures=[],
        ),
        EntryInput(
            guid="rss-2",
            url="https://example.com/2",
            title="Second post",
            author=None,
            published_at="2026-01-02T12:30:00Z",
            summary="Only summary",
            content=None,
            enclosures=[],
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
            enclosures=[],
        ),
        EntryInput(
            guid="urn:atom:2",
            url="https://example.com/atom-2",
            title="Updated only",
            author=None,
            published_at="2026-03-04T05:06:07Z",
            summary="Later summary",
            content=None,
            enclosures=[],
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
            enclosures=[],
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
            enclosures=[],
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


@pytest.mark.asyncio
async def test_fetch_feed_does_not_open_a_path_shaped_body(tmp_path, monkeypatch):
    # feedparser.parse(bytes) opens an existing path and returns that file.
    local = tmp_path / "secret.xml"
    local.write_bytes(
        b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>LOCAL FILE</title>
<item><title>Secret</title><guid>local-guid</guid>
<link>https://local.example/secret</link></item>
</channel></rss>"""
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=os.fsencode(local))

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
    assert result.feed_title != "LOCAL FILE"


@pytest.mark.asyncio
async def test_fetch_feed_resolves_relative_links_against_the_final_url(monkeypatch):
    # Keeps "posts/1", or joins it to the URL from before the redirect.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"Location": "https://example.com/blog/feed.xml"},
            )
        return httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
<item><title>Relative</title><guid>rel-1</guid><link>posts/1</link></item>
<item><title>Absolute</title><guid>abs-1</guid>
<link>https://cdn.example.com/a</link></item>
</channel></rss>""",
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
    assert result.error is None
    assert [(entry.guid, entry.url) for entry in result.entries] == [
        ("rel-1", "https://example.com/blog/posts/1"),
        ("abs-1", "https://cdn.example.com/a"),
    ]


@pytest.mark.asyncio
async def test_fetch_feed_returns_error_for_non_ascii_user_agent(monkeypatch):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, content=RSS)

    _install(monkeypatch, handler)
    result = await fetch_feed(
        "https://example.com/rss.xml",
        etag=None,
        last_modified=None,
        user_agent="阅读器/1.0",
        entry_cap=10,
    )
    assert result.status == "error"
    assert result.entries == []
    assert called["n"] == 0


_RSS_BODY = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>Discovered Feed</title>
<item><title>One</title><link>https://example.com/1</link></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_discover_feeds_reads_advertised_link_tags(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    html = """<html><head>
    <link rel="alternate" type="application/rss+xml" title="Main" href="/blog/feed.xml">
    <link rel="alternate" type="application/atom+xml" href="https://other.example.com/atom">
    <link rel="stylesheet" href="/style.css">
    </head><body>hi</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/blog/"
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/blog/", user_agent="test")
    assert candidates == [
        {"url": "https://example.com/blog/feed.xml", "type": "rss"},
        {"url": "https://other.example.com/atom", "type": "atom"},
    ]


@pytest.mark.asyncio
async def test_discover_feeds_accepts_a_direct_feed_url(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_RSS_BODY, headers={"content-type": "application/rss+xml"})

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/feed.xml", user_agent="test")
    assert candidates == [
        {"url": "https://example.com/feed.xml", "type": "rss", "title": "Discovered Feed"}
    ]


@pytest.mark.asyncio
async def test_discover_feeds_probes_common_paths_when_unadvertised(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(200, content=_RSS_BODY)
        if request.url.path == "/":
            return httpx.Response(200, text="<html><body>no links</body></html>")
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/", user_agent="test")
    assert candidates == [
        {"url": "https://example.com/feed", "type": "rss", "title": "Discovered Feed"}
    ]


@pytest.mark.asyncio
async def test_discover_feeds_returns_empty_when_nothing_found(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<html><body>plain</body></html>")
        return httpx.Response(404)

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/", user_agent="test") == []


@pytest.mark.asyncio
async def test_discover_feeds_returns_empty_on_fetch_error(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/", user_agent="test") == []


_ATOM_BODY = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>E</title><id>urn:e1</id></entry></feed>"""


@pytest.mark.asyncio
async def test_discover_feeds_accepts_bom_prefixed_direct_feed(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/custom-feed.php":
            return httpx.Response(200, content=b"\xef\xbb\xbf" + _RSS_BODY)
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/custom-feed.php", user_agent="test")
    assert candidates == [
        {"url": "https://example.com/custom-feed.php", "type": "rss", "title": "Discovered Feed"}
    ]


@pytest.mark.asyncio
async def test_discover_feeds_accepts_prolog_less_atom_and_rss(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/atom":
            return httpx.Response(200, content=_ATOM_BODY)
        if request.url.path == "/rss":
            return httpx.Response(200, content=b'<rss version="2.0"><channel><title>T</title></channel></rss>')
        return httpx.Response(404)

    _install(monkeypatch, handler)
    atom = await discover_feeds("https://example.com/atom", user_agent="test")
    assert atom == [{"url": "https://example.com/atom", "type": "atom", "title": "Atom Feed"}]
    rss = await discover_feeds("https://example.com/rss", user_agent="test")
    assert rss[0]["type"] == "rss"


@pytest.mark.asyncio
async def test_discover_feeds_accepts_rdf_rss10(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/"><channel><title>RDF</title></channel>
<item><title>I</title><link>https://example.com/i</link></item></rdf:RDF>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rdf.xml":
            return httpx.Response(200, content=rdf)
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/rdf.xml", user_agent="test")
    assert len(candidates) == 1
    assert candidates[0]["type"] == "rss"


@pytest.mark.asyncio
async def test_discover_feeds_caps_oversized_homepage(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    monkeypatch.setattr("rss_mcp.feed_client.MAX_FEED_BYTES", 16)
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/", user_agent="test") == []


@pytest.mark.asyncio
async def test_discover_feeds_skips_oversized_probe(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    monkeypatch.setattr("rss_mcp.feed_client.MAX_FEED_BYTES", 200)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<html>plain</html>")
        if request.url.path == "/feed":
            return httpx.Response(200, content=b"x" * 300)
        if request.url.path == "/rss.xml":
            return httpx.Response(200, content=_RSS_BODY)
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/", user_agent="test")
    assert [c["url"] for c in candidates] == ["https://example.com/rss.xml"]


@pytest.mark.asyncio
async def test_discover_feeds_returns_empty_on_invalid_url(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    _install(monkeypatch, handler)
    assert await discover_feeds("http://example.com:abc", user_agent="test") == []


@pytest.mark.asyncio
async def test_discover_feeds_returns_empty_for_non_ascii_user_agent(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/", user_agent="阅读器/1.0") == []


@pytest.mark.asyncio
async def test_discover_feeds_ignores_error_pages_with_link_tags(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=html)

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/missing", user_agent="test") == []


@pytest.mark.asyncio
async def test_discover_feeds_rejects_soft_200_html_probes(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>SPA catch-all</body></html>")

    _install(monkeypatch, handler)
    assert await discover_feeds("https://example.com/", user_agent="test") == []


@pytest.mark.asyncio
async def test_discover_feeds_tolerates_probe_errors(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<html>plain</html>")
        if request.url.path == "/feed":
            raise httpx.ConnectError("reset", request=request)
        if request.url.path == "/rss.xml":
            return httpx.Response(200, content=_RSS_BODY)
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/", user_agent="test")
    assert [c["url"] for c in candidates] == ["https://example.com/rss.xml"]


@pytest.mark.asyncio
async def test_discover_feeds_merges_multiple_probes_in_order(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<html>plain</html>")
        if request.url.path == "/rss.xml":
            return httpx.Response(200, content=_RSS_BODY)
        if request.url.path == "/atom.xml":
            return httpx.Response(200, content=_ATOM_BODY)
        return httpx.Response(404)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/", user_agent="test")
    assert [(c["url"], c["type"]) for c in candidates] == [
        ("https://example.com/rss.xml", "rss"),
        ("https://example.com/atom.xml", "atom"),
    ]


@pytest.mark.asyncio
async def test_discover_feeds_dedupes_relative_and_absolute_links(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    html = """<html><head>
    <link rel="alternate" type="application/rss+xml" href="/blog/feed.xml" />
    <link rel="alternate" type="application/rss+xml" href="https://example.com/blog/feed.xml">
    </head></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/blog/", user_agent="test")
    assert candidates == [{"url": "https://example.com/blog/feed.xml", "type": "rss"}]


@pytest.mark.asyncio
async def test_discover_feeds_resolves_against_redirect_target(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/blog/feed.xml"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://example.com/"})
        return httpx.Response(200, text=html)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("http://example.com/", user_agent="test")
    assert candidates == [{"url": "https://example.com/blog/feed.xml", "type": "rss"}]


@pytest.mark.asyncio
async def test_discover_feeds_honors_base_href(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    html = """<html><head><base href="https://example.com/">
    <link rel="alternate" type="application/rss+xml" href="feed.xml">
    </head></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/blog/post"
        return httpx.Response(200, text=html)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/blog/post", user_agent="test")
    assert candidates == [{"url": "https://example.com/feed.xml", "type": "rss"}]


@pytest.mark.asyncio
async def test_discover_feeds_caps_candidate_count(monkeypatch):
    from rss_mcp.feed_client import discover_feeds

    links = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="/feed{i}.xml">'
        for i in range(60)
    )
    html = f"<html><head>{links}</head></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    _install(monkeypatch, handler)
    candidates = await discover_feeds("https://example.com/", user_agent="test")
    assert len(candidates) == 50
