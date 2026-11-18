import xml.etree.ElementTree as ET
from pathlib import Path

from rss_mcp.models import Subscription
from rss_mcp.opml import build_opml, parse_opml

FIXTURE = (Path(__file__).parent / "fixtures" / "sample.opml").read_text(encoding="utf-8")


def _sub(url: str, title: str | None, id: int) -> Subscription:
    return Subscription(
        id=id,
        url=url,
        title=title,
        tags=["ignored"],
        etag="etag",
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        last_fetched_at="2026-01-01T00:00:00Z",
        last_error=None,
        created_at="2026-01-01T00:00:00Z",
    )


def test_parse_opml_reads_nested_feeds_and_skips_folders():
    # Drops a nested feed, keeps a folder, or leaves xmlUrl whitespace unstripped.
    assert parse_opml(FIXTURE) == [
        ("https://example.com/feed.xml", "Example", ["News"]),
        ("https://example.com/untitled.xml", None, ["News"]),
        ("https://example.com/text-only.xml", "Only Text", []),
        ("https://example.com/atom.xml?a=1&b=2", "Tom & Jerry", []),
    ]


def test_build_opml_emits_feed_outlines():
    # Omits xmlUrl, drops a title, or treats a missing title as the url label twice.
    xml = build_opml(
        [
            _sub("https://example.com/feed.xml?a=1&b=2", "Tom & Jerry", 1),
            _sub("https://example.com/untitled.xml", "  ", 2),
        ]
    )
    root = ET.fromstring(xml)
    assert root.tag == "opml"
    outlines = [el for el in root.iter("outline") if el.get("xmlUrl")]
    assert [(el.get("xmlUrl"), el.get("title"), el.get("text")) for el in outlines] == [
        ("https://example.com/feed.xml?a=1&b=2", "Tom & Jerry", "Tom & Jerry"),
        ("https://example.com/untitled.xml", None, "https://example.com/untitled.xml"),
    ]


def test_opml_round_trip_preserves_url_and_title():
    # Rebuilds titles from urls, or reorders feeds.
    subs = [
        _sub("https://example.com/feed.xml", "Example", 1),
        _sub("https://example.com/untitled.xml", None, 2),
        _sub("https://example.com/atom.xml?a=1&b=2", "Tom & Jerry", 3),
    ]
    assert parse_opml(build_opml(subs)) == [
        ("https://example.com/feed.xml", "Example", ["ignored"]),
        ("https://example.com/untitled.xml", None, ["ignored"]),
        ("https://example.com/atom.xml?a=1&b=2", "Tom & Jerry", ["ignored"]),
    ]


def test_build_opml_groups_by_first_tag():
    tech = _sub("https://example.com/tech.xml", "Tech", 1)
    tech.tags = ["技术", "blog"]
    news = _sub("https://example.com/news.xml", "News", 2)
    news.tags = ["资讯"]
    plain = _sub("https://example.com/plain.xml", None, 3)
    plain.tags = []
    root = ET.fromstring(build_opml([tech, news, plain]))
    body = root.find("body")
    folders = [el for el in body if not el.get("xmlUrl")]
    assert [el.get("text") for el in folders] == ["技术", "资讯"]
    assert folders[0].find("outline").get("xmlUrl") == "https://example.com/tech.xml"
    assert body[-1].get("xmlUrl") == "https://example.com/plain.xml"
