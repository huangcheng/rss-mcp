from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
_WHITESPACE = re.compile(r"\s+")


def canonical_key(url: str | None, title: str | None) -> str | None:
    """Dedup identity: normalized URL when present, else normalized title.

    Normalization unifies the scheme to https, lowercases the host, drops
    fragments and tracking parameters, and strips trailing slashes so
    syndicated copies of the same article across feeds collapse to one key.
    """
    if url:
        stripped = url.strip()
        if stripped:
            parts = urlsplit(stripped)
            if parts.scheme and parts.netloc:
                query = [
                    (key, value)
                    for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if key.lower() not in _TRACKING_PARAMS
                ]
                path = parts.path.rstrip("/") or "/"
                return urlunsplit(
                    ("https", parts.netloc.lower(), path, urlencode(sorted(query)), "")
                )
    if title:
        folded = _WHITESPACE.sub(" ", title.strip()).lower()
        if folded:
            return "title:" + folded
    return None