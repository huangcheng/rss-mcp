from __future__ import annotations

import xml.etree.ElementTree as ET

from rss_mcp.models import Subscription


def _local(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _attr(element: ET.Element, name: str) -> str | None:
    value = element.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _title(element: ET.Element, url: str) -> str | None:
    title = _attr(element, "title")
    if title is not None:
        return title
    text = _attr(element, "text")
    if text is None or text == url:
        return None
    return text


def parse_opml(xml: str) -> list[tuple[str, str | None]]:
    root = ET.fromstring(xml)
    feeds: list[tuple[str, str | None]] = []
    for element in root.iter():
        if _local(element.tag) != "outline":
            continue
        url = _attr(element, "xmlUrl")
        if url is None:
            continue
        feeds.append((url, _title(element, url)))
    return feeds


def build_opml(subs: list[Subscription]) -> str:
    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "rss-mcp subscriptions"
    body = ET.SubElement(opml, "body")
    for sub in subs:
        url = sub.url.strip()
        title = (sub.title or "").strip() or None
        attrs = {"type": "rss", "xmlUrl": url, "text": title or url}
        if title is not None:
            attrs["title"] = title
        ET.SubElement(body, "outline", attrs)
    ET.indent(opml)
    return ET.tostring(opml, encoding="unicode", xml_declaration=True)
