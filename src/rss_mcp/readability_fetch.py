from __future__ import annotations

import re

import httpx
from lxml import html as lxml_html
from readability import Document

MAX_ARTICLE_BYTES = 5 * 1024 * 1024
_WHITESPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_HTML_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
    }
)


def _media_type(response: httpx.Response) -> str | None:
    raw = response.headers.get("content-type")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.split(";", 1)[0].strip().lower() or None


def _allows_extract(response: httpx.Response) -> bool:
    media = _media_type(response)
    if media is None:
        return True
    return media in _HTML_TYPES or media.endswith("+xml")


def _looks_textual(body: bytes) -> bool:
    sample = body[:4096]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return b"\x00" not in sample


async def _read_limited(response: httpx.Response) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_ARTICLE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _separate_blocks(root) -> None:
    for element in root.iter():
        tag = element.tag
        if not isinstance(tag, str) or tag.lower() not in _BLOCK_TAGS:
            continue
        element.tail = "\n" + (element.tail or "")


def _plain_text(summary_html: str) -> str | None:
    root = lxml_html.fromstring(summary_html)
    _separate_blocks(root)
    text = root.text_content().replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    return cleaned or None


async def fetch_article_text(
    url: str,
    *,
    user_agent: str,
    timeout: float = 30.0,
) -> str | None:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200 or not _allows_extract(response):
                    return None
                body = await _read_limited(response)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeEncodeError):
        return None
    if not body or not body.strip() or not _looks_textual(body):
        return None
    try:
        summary = Document(body, url=url).summary()
        return _plain_text(summary)
    except Exception:
        return None
