from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from rss_mcp.models import Entry, EntryInput, Subscription

_ENTRY_LIMIT_MAX = 100

_ENTRY_COLUMNS = """
    e.id,
    e.subscription_id,
    e.guid,
    e.url,
    e.title,
    e.author,
    e.published_at,
    e.summary,
    e.content,
    e.fetched_at,
    es.read_at,
    COALESCE(es.starred, 0) AS starred
"""

_ENTRY_FROM = """
FROM entries AS e
LEFT JOIN entry_state AS es ON es.entry_id = e.id
"""

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


def _clamp_limit(limit: int) -> int:
    if limit < 0:
        return 0
    if limit > _ENTRY_LIMIT_MAX:
        return _ENTRY_LIMIT_MAX
    return limit


def _normalized_url(url: str | None) -> str | None:
    if url is None:
        return None
    stripped = url.strip()
    return stripped or None


def _entry_guid(item: EntryInput) -> str:
    guid = (item.guid or "").strip()
    if guid:
        return guid
    url = _normalized_url(item.url)
    if url:
        return "url:" + url
    title = (item.title or "").strip()
    digest = hashlib.sha256(title.encode()).hexdigest()
    return "title:" + digest


def _entry_from_row(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        subscription_id=row["subscription_id"],
        guid=row["guid"],
        url=row["url"],
        title=row["title"],
        author=row["author"],
        published_at=row["published_at"],
        summary=row["summary"],
        content=row["content"],
        fetched_at=row["fetched_at"],
        read_at=row["read_at"],
        starred=bool(row["starred"]),
    )


def _fts_match_query(query: str) -> str | None:
    text = query.strip()
    if not text or not text.strip('"'):
        return None
    return '"' + text.replace('"', '""') + '"'


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

    def upsert_entries(self, subscription_id: int, items: list[EntryInput]) -> int:
        written = 0
        for item in items:
            cursor = self._conn.execute(
                """INSERT INTO entries (
                       subscription_id, guid, url, title, author,
                       published_at, summary, content, fetched_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subscription_id, guid) DO UPDATE SET
                       url = excluded.url,
                       title = excluded.title,
                       author = excluded.author,
                       published_at = excluded.published_at,
                       summary = excluded.summary,
                       content = COALESCE(excluded.content, entries.content),
                       fetched_at = excluded.fetched_at
                   WHERE entries.url IS NOT excluded.url
                      OR entries.title IS NOT excluded.title
                      OR entries.author IS NOT excluded.author
                      OR entries.published_at IS NOT excluded.published_at
                      OR entries.summary IS NOT excluded.summary
                      OR (
                          excluded.content IS NOT NULL
                          AND entries.content IS NOT excluded.content
                      )""",
                (
                    subscription_id,
                    _entry_guid(item),
                    _normalized_url(item.url),
                    item.title,
                    item.author,
                    item.published_at,
                    item.summary,
                    item.content,
                    _utc_now(),
                ),
            )
            if cursor.rowcount > 0:
                written += cursor.rowcount
        self._conn.commit()
        return written

    def list_entries(
        self,
        *,
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int = 20,
        since: str | None = None,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[object] = []
        if subscription_id is not None:
            clauses.append("e.subscription_id = ?")
            params.append(subscription_id)
        if unread_only:
            clauses.append("es.read_at IS NULL")
        if since is not None:
            clauses.append("e.published_at >= ?")
            params.append(since)
        params.append(_clamp_limit(limit))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""SELECT {_ENTRY_COLUMNS}
                {_ENTRY_FROM}
                {where}
                ORDER BY e.published_at DESC, e.id DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self._conn.execute(
            f"""SELECT {_ENTRY_COLUMNS}
                {_ENTRY_FROM}
                WHERE e.id = ?""",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        return _entry_from_row(row)

    def mark_entries(self, entry_ids: list[int], read: bool) -> int:
        changed = 0
        read_at = _utc_now() if read else None
        seen: set[int] = set()
        for entry_id in entry_ids:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            row = self._conn.execute(
                """SELECT e.id, es.read_at
                   FROM entries AS e
                   LEFT JOIN entry_state AS es ON es.entry_id = e.id
                   WHERE e.id = ?""",
                (entry_id,),
            ).fetchone()
            if row is None or (row["read_at"] is not None) == read:
                continue
            self._conn.execute(
                """INSERT INTO entry_state (entry_id, read_at)
                   VALUES (?, ?)
                   ON CONFLICT(entry_id) DO UPDATE SET read_at = excluded.read_at""",
                (entry_id, read_at),
            )
            changed += 1
        self._conn.commit()
        return changed

    def search_entries(
        self,
        query: str,
        *,
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int = 20,
    ) -> list[Entry]:
        match = _fts_match_query(query)
        if match is None:
            return []
        clauses = ["entries_fts MATCH ?"]
        params: list[object] = [match]
        if subscription_id is not None:
            clauses.append("e.subscription_id = ?")
            params.append(subscription_id)
        if unread_only:
            clauses.append("es.read_at IS NULL")
        params.append(_clamp_limit(limit))
        try:
            rows = self._conn.execute(
                f"""SELECT {_ENTRY_COLUMNS}
                    FROM entries AS e
                    JOIN entries_fts ON entries_fts.rowid = e.id
                    LEFT JOIN entry_state AS es ON es.entry_id = e.id
                    WHERE {" AND ".join(clauses)}
                    ORDER BY bm25(entries_fts), e.published_at DESC, e.id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_entry_from_row(row) for row in rows]

    def update_entry_content(self, entry_id: int, content: str) -> None:
        self._conn.execute(
            "UPDATE entries SET content = ? WHERE id = ?",
            (content, entry_id),
        )
        self._conn.commit()
