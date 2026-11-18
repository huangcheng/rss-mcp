import httpx
import pytest

import rss_mcp.readability_fetch as readability_fetch
from rss_mcp.readability_fetch import fetch_article_text

MINIFIED = (
    "<!DOCTYPE html><html><head><title>Min</title></head><body>"
    '<article class="entry-content"><h1>Title</h1><p>First paragraph stays whole.</p>'
    "<p>Second paragraph follows.<br>After the break.</p></article>"
    "</body></html>"
)

ARTICLE = """<!DOCTYPE html>
<html>
<head><title>Orbital pass</title></head>
<body>
<div class="sidebar">
<p>Sidebar promo about unrelated gadgets and newsletter signup links.</p>
</div>
<article class="entry-content">
<h1>Orbital pass</h1>
<p>The unique sentence about orbital mechanics appears only in the article body.</p>
<p>A second paragraph continues with satellites, ground stations, and the evening pass over the city.</p>
<p>A third paragraph mentions the antenna calibration and the operator who logged the pass after midnight.</p>
</article>
</body>
</html>
"""


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rss_mcp.readability_fetch.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_fetch_article_text_returns_article_and_sends_request_options(monkeypatch):
    # Returns the sidebar, drops the article sentence, or ignores user-agent/timeout.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers["user-agent"]
        seen["read_timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(
            200,
            content=ARTICLE.encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _install(monkeypatch, handler)
    text = await fetch_article_text(
        "https://example.com/posts/1",
        user_agent="rss-mcp/test",
        timeout=12.5,
    )

    assert seen == {"user_agent": "rss-mcp/test", "read_timeout": 12.5}
    assert text is not None
    assert "orbital mechanics" in text
    assert "Sidebar promo" not in text


@pytest.mark.asyncio
async def test_fetch_article_text_splits_minified_blocks(monkeypatch):
    # Joins a heading, paragraphs, and br into TitleFirst / follows.After.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=MINIFIED.encode(),
            headers={"content-type": "text/html"},
        )

    _install(monkeypatch, handler)
    text = await fetch_article_text(
        "https://example.com/posts/min",
        user_agent="rss-mcp/test",
    )

    assert text is not None
    assert "TitleFirst" not in text
    assert "whole.Second" not in text
    assert "follows.After" not in text
    assert "First paragraph stays whole." in text
    assert "Second paragraph follows." in text
    assert "After the break." in text


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_for_non_ascii_user_agent(monkeypatch):
    # Lets UnicodeEncodeError escape for a User-Agent httpx cannot encode.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="should not be fetched")

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/posts/1", user_agent="阅读器/1.0")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_on_http_error(monkeypatch):
    # Raises, or returns the error page body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="unavailable")

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/missing", user_agent="rss-mcp/test")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_on_network_failure(monkeypatch):
    # Lets ConnectError propagate.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/down", user_agent="rss-mcp/test")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_on_empty_body(monkeypatch):
    # Returns an empty string instead of None.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/empty", user_agent="rss-mcp/test")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,body",
    [
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        ("application/json", b'{"a":1}'),
    ],
)
async def test_fetch_article_text_returns_none_for_non_html_content_type(
    monkeypatch, content_type, body
):
    # Returns decoded bytes from a non-HTML 200.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/file", user_agent="rss-mcp/test")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_for_binary_without_content_type(monkeypatch):
    # Decodes a NUL-bearing body into article text.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/raw", user_agent="rss-mcp/test")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_article_text_returns_none_when_body_exceeds_cap(monkeypatch):
    # Still extracts the article when the body is over the cap.
    monkeypatch.setattr(readability_fetch, "MAX_ARTICLE_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ARTICLE.encode(),
            headers={"content-type": "text/html"},
        )

    _install(monkeypatch, handler)
    assert (
        await fetch_article_text("https://example.com/posts/1", user_agent="rss-mcp/test")
        is None
    )
