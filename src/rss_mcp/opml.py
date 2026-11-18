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


def parse_opml(xml: str) -> list[tuple[str, str | None, list[str]]]:
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    feeds: list[tuple[str, str | None, list[str]]] = []
    for element in root.iter():
        if _local(element.tag) != "outline":
            continue
        url = _attr(element, "xmlUrl")
        if url is None:
            continue
        tags: list[str] = []
        ancestor = parents.get(element)
        while ancestor is not None and _local(ancestor.tag) == "outline":
            label = _attr(ancestor, "title") or _attr(ancestor, "text")
            if label:
                tags.append(label)
            ancestor = parents.get(ancestor)
        tags.reverse()
        feeds.append((url, _title(element, url), tags))
    return feeds


def build_opml(subs: list[Subscription]) -> str:
    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "rss-mcp subscriptions"
    body = ET.SubElement(opml, "body")
    categories: dict[str, ET.Element] = {}
    for sub in subs:
        url = sub.url.strip()
        title = (sub.title or "").strip() or None
        attrs = {"type": "rss", "xmlUrl": url, "text": title or url}
        if title is not None:
            attrs["title"] = title
        parent = body
        if sub.tags:
            tag = sub.tags[0]
            if tag not in categories:
                categories[tag] = ET.SubElement(body, "outline", {"text": tag, "title": tag})
            parent = categories[tag]
        ET.SubElement(parent, "outline", attrs)
    ET.indent(opml)
    return ET.tostring(opml, encoding="unicode", xml_declaration=True)