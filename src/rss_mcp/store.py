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
    e.enclosures,
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
    s.user_agent,
    s.headers,
    s.failure_count,
    s.disabled,
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
    keys = row.keys()
    raw_enclosures = row["enclosures"] if "enclosures" in keys else None
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
        enclosures=json.loads(raw_enclosures) if raw_enclosures else [],
        snippet=row["snippet"] if "snippet" in keys else None,
    )


def _fts_match_query(query: str) -> str | None:
    terms: list[str] = []
    for raw in query.split():
        token = raw.strip('"')
        if not token:
            continue
        terms.append('"' + token.replace('"', '""') + '"')
    if not terms:
        return None
    return " AND ".join(terms)


def _subscription_from_row(row: sqlite3.Row) -> Subscription:
    raw_headers = row["headers"]
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
        user_agent=row["user_agent"],
        headers=json.loads(raw_headers) if raw_headers else None,
        failure_count=row["failure_count"],
        disabled=bool(row["disabled"]),
    )


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_subscription(
        self,
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Subscription:
        normalized = url.strip()
        self._conn.execute(
            """INSERT OR IGNORE INTO subscriptions (url, title, tags, created_at, user_agent, headers)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                normalized,
                title,
                json.dumps(tags or []),
                _utc_now(),
                user_agent,
                json.dumps(headers) if headers else None,
            ),
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

    def list_subscriptions(self, tag: str | None = None) -> list[Subscription]:
        sql = _SUBSCRIPTION_SQL
        params: list[object] = []
        if tag is not None:
            sql += " WHERE EXISTS (SELECT 1 FROM json_each(s.tags) WHERE value = ?)"
            params.append(tag)
        rows = self._conn.execute(sql + " ORDER BY s.id", params).fetchall()
        return [_subscription_from_row(row) for row in rows]

    def update_subscription(
        self,
        subscription_id: int,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        disabled: bool | None = None,
    ) -> bool:
        assignments: list[str] = []
        params: list[object] = []
        if title is not None:
            assignments.append("title = ?")
            params.append(title.strip() or None)
        if tags is not None:
            assignments.append("tags = ?")
            params.append(json.dumps(tags))
        if user_agent is not None:
            assignments.append("user_agent = ?")
            params.append(user_agent.strip() or None)
        if headers is not None:
            assignments.append("headers = ?")
            params.append(json.dumps(headers) if headers else None)
        if disabled is not None:
            assignments.append("disabled = ?")
            params.append(int(disabled))
            if not disabled:
                assignments.append("failure_count = 0")
                assignments.append("last_error = NULL")
        if not assignments:
            return False
        params.append(subscription_id)
        cursor = self._conn.execute(
            f"UPDATE subscriptions SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def record_fetch_failure(self, subscription_id: int, error: str) -> int:
        self._conn.execute(
            """UPDATE subscriptions
               SET last_error = ?, failure_count = failure_count + 1, last_fetched_at = ?
               WHERE id = ?""",
            (error, _utc_now(), subscription_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT failure_count FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
        return int(row["failure_count"]) if row else 0

    def reset_failures(self, subscription_id: int) -> None:
        self._conn.execute(
            "UPDATE subscriptions SET failure_count = 0, last_error = NULL WHERE id = ?",
            (subscription_id,),
        )
        self._conn.commit()

    def set_disabled(self, subscription_id: int, disabled: bool) -> None:
        self._conn.execute(
            "UPDATE subscriptions SET disabled = ? WHERE id = ?",
            (int(disabled), subscription_id),
        )
        self._conn.commit()

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
                       published_at, summary, content, fetched_at, enclosures
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subscription_id, guid) DO UPDATE SET
                       url = excluded.url,
                       title = excluded.title,
                       author = excluded.author,
                       published_at = excluded.published_at,
                       summary = excluded.summary,
                       content = COALESCE(excluded.content, entries.content),
                       fetched_at = excluded.fetched_at,
                       enclosures = COALESCE(excluded.enclosures, entries.enclosures)
                   WHERE entries.url IS NOT excluded.url
                      OR entries.title IS NOT excluded.title
                      OR entries.author IS NOT excluded.author
                      OR entries.published_at IS NOT excluded.published_at
                      OR entries.summary IS NOT excluded.summary
                      OR (
                          excluded.content IS NOT NULL
                          AND entries.content IS NOT excluded.content
                      )
                      OR (
                          excluded.enclosures IS NOT NULL
                          AND entries.enclosures IS NOT excluded.enclosures
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
                    json.dumps(item.enclosures) if item.enclosures else None,
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
                f"""SELECT {_ENTRY_COLUMNS},
                           snippet(entries_fts, -1, '[', ']', '…', 12) AS snippet
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

    def mark_subscription_entries(self, subscription_id: int, read: bool) -> int:
        rows = self._conn.execute(
            "SELECT id FROM entries WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchall()
        return self.mark_entries([row["id"] for row in rows], read)

    def prune_entries(self, cutoff_iso: str) -> int:
        cursor = self._conn.execute(
            """DELETE FROM entries
               WHERE COALESCE(published_at, fetched_at) < ?
                 AND id NOT IN (SELECT entry_id FROM entry_state WHERE starred = 1)""",
            (cutoff_iso,),
        )
        self._conn.commit()
        return cursor.rowcount

    def update_entry_content(self, entry_id: int, content: str) -> None:
        self._conn.execute(
            "UPDATE entries SET content = ? WHERE id = ?",
            (content, entry_id),
        )
        self._conn.commit()

    def set_title_if_empty(self, subscription_id: int, title: str) -> None:
        cleaned = title.strip()
        if not cleaned:
            return
        self._conn.execute(
            """UPDATE subscriptions
               SET title = ?
               WHERE id = ?
                 AND (title IS NULL OR trim(title) = '')""",
            (cleaned, subscription_id),
        )
        self._conn.commit()
