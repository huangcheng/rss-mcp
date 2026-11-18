from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from rss_mcp.models import Subscription

_SUBSCRIPTION_SQL = """
SELECT
    s.id,
    s.url,
    s.title,
    s.tags,
    s.etag,
    s.last_modified,
    s.last_fetched_at,
    s.last_error,
    s.created_at,
    (
        SELECT COUNT(*)
        FROM entries AS e
        LEFT JOIN entry_state AS es ON es.entry_id = e.id
        WHERE e.subscription_id = s.id
          AND es.read_at IS NULL
    ) AS unread_count
FROM subscriptions AS s
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags_from_column(raw: str | None) -> list[str]:
    if not raw:
        return []
    loaded = json.loads(raw)
    return list(loaded)


def _subscription_from_row(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        tags=_tags_from_column(row["tags"]),
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_fetched_at=row["last_fetched_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        unread_count=row["unread_count"],
    )


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_subscription(
        self,
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> Subscription:
        normalized = url.strip()
        self._conn.execute(
            """INSERT OR IGNORE INTO subscriptions (url, title, tags, created_at)
               VALUES (?, ?, ?, ?)""",
            (normalized, title, json.dumps(tags or []), _utc_now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            _SUBSCRIPTION_SQL + " WHERE s.url = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"subscription missing after insert: {normalized}")
        return _subscription_from_row(row)

    def remove_subscription(self, subscription_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM subscriptions WHERE id = ?",
            (subscription_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_subscriptions(self) -> list[Subscription]:
        rows = self._conn.execute(_SUBSCRIPTION_SQL + " ORDER BY s.id").fetchall()
        return [_subscription_from_row(row) for row in rows]

    def get_subscription(self, subscription_id: int) -> Subscription | None:
        row = self._conn.execute(
            _SUBSCRIPTION_SQL + " WHERE s.id = ?",
            (subscription_id,),
        ).fetchone()
        if row is None:
            return None
        return _subscription_from_row(row)

    def update_fetch_meta(
        self,
        subscription_id: int,
        *,
        etag: str | None,
        last_modified: str | None,
        last_fetched_at: str | None,
        last_error: str | None,
    ) -> None:
        self._conn.execute(
            """UPDATE subscriptions
               SET etag = ?, last_modified = ?, last_fetched_at = ?, last_error = ?
               WHERE id = ?""",
            (etag, last_modified, last_fetched_at, last_error, subscription_id),
        )
        self._conn.commit()
