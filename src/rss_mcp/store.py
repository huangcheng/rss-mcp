from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from rss_mcp.canon import canonical_key
from rss_mcp.dialect import dialect_for
from rss_mcp.models import Entry, EntryInput, Subscription

_ENTRY_LIMIT_MAX = 100
_DEDUP_WINDOW_DAYS = 30

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
    e.duplicate_of,
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
    s.muted,
    s.include_keywords,
    s.exclude_keywords,
    s.fetch_interval,
    s.ttl,
    s.skip_hours,
    s.skip_days,
    s.last_item_count,
    (
        SELECT COUNT(*)
        FROM entries AS e
        LEFT JOIN entry_state AS es ON es.entry_id = e.id
        WHERE e.subscription_id = s.id
          AND es.read_at IS NULL
          AND e.duplicate_of IS NULL
          AND e.filtered = 0
          AND s.muted = 0
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


def _clean_keywords(keywords: list[str] | None) -> list[str]:
    if not keywords:
        return []
    return [keyword.strip() for keyword in keywords if keyword.strip()]


def _clean_interval(value: int | None) -> int | None:
    return value if value and value > 0 else None


def _entry_filtered(
    include: list[str], exclude: list[str], title: str | None, summary: str | None
) -> bool:
    # Case-insensitive substring match over title + summary. An empty include
    # list passes everything; a non-empty one requires at least one hit.
    haystack = ((title or "") + "\n" + (summary or "")).casefold()
    if any(keyword.casefold() in haystack for keyword in exclude):
        return True
    if include:
        return not any(keyword.casefold() in haystack for keyword in include)
    return False


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


def _entry_from_row(row: Any) -> Entry:
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
        duplicate_of=row["duplicate_of"] if "duplicate_of" in keys else None,
    )


def _subscription_from_row(row: Any) -> Subscription:
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
        muted=bool(row["muted"]),
        include_keywords=_tags_from_column(row["include_keywords"]),
        exclude_keywords=_tags_from_column(row["exclude_keywords"]),
        fetch_interval=row["fetch_interval"],
        ttl=row["ttl"],
        skip_hours=json.loads(row["skip_hours"]) if row["skip_hours"] else [],
        skip_days=_tags_from_column(row["skip_days"]),
        last_item_count=row["last_item_count"],
    )


class Store:
    """Engine-agnostic storage; SQL dialect differences live in the dialect layer."""

    def __init__(self, conn: Any, *, maintain: bool = True) -> None:
        self._conn = conn
        self._dialect = dialect_for(conn)
        if not maintain:
            # Read-only consumers (metrics scrapes) skip the healing passes:
            # they are idempotent, already run at startup, and cost a full
            # table scan each time.
            return
        self._backfill_canonical_keys()
        # Heal shadowed keepers first (e.g. a crash between rule change and
        # re-election), then converge duplicate links.
        self._release_shadowed_keepers()
        self._conn.commit()
        self._reelect_keepers()

    def _backfill_canonical_keys(self) -> None:
        rows = self._conn.execute(
            "SELECT id, url, title FROM entries WHERE canonical_url IS NULL"
        ).fetchall()
        if not rows:
            return
        # Sentinel '' for keyless rows so the NULL scan converges; no real
        # canonical_key() result is ever empty.
        updates = [(canonical_key(row["url"], row["title"]) or "", row["id"]) for row in rows]
        self._conn.executemany("UPDATE entries SET canonical_url = ? WHERE id = ?", updates)
        self._conn.commit()

    def _release_shadowed_keepers(self) -> None:
        # A keeper readers cannot see (keyword-filtered entry, muted feed) must
        # not suppress the visible copies of the same article.
        self._conn.execute(
            """UPDATE entries SET duplicate_of = NULL
               WHERE duplicate_of IS NOT NULL
                 AND duplicate_of IN (
                     SELECT k.id FROM entries AS k
                     JOIN subscriptions AS s ON s.id = k.subscription_id
                     WHERE k.filtered = 1 OR s.muted = 1
                 )"""
        )

    def _reelect_keepers(self) -> None:
        # After a keeper dies (FK SET NULL) several copies can be revealed at
        # once; re-elect the earliest surviving cross-feed copy per key. URL
        # identity is authoritative here, so no time window is applied.
        # Candidates must be visible: unfiltered and not in a muted feed.
        self._conn.execute(
            """UPDATE entries SET duplicate_of = (
                   SELECT MIN(e2.id) FROM entries AS e2
                   JOIN subscriptions AS s2 ON s2.id = e2.subscription_id
                   WHERE e2.canonical_url = entries.canonical_url
                     AND e2.id < entries.id
                     AND e2.duplicate_of IS NULL
                     AND e2.filtered = 0
                     AND s2.muted = 0
                     AND e2.subscription_id != entries.subscription_id
               )
               WHERE duplicate_of IS NULL
                 AND canonical_url IS NOT NULL
                 AND canonical_url != ''
                 AND EXISTS (
                   SELECT 1 FROM entries AS e2
                   JOIN subscriptions AS s2 ON s2.id = e2.subscription_id
                   WHERE e2.canonical_url = entries.canonical_url
                     AND e2.id < entries.id
                     AND e2.duplicate_of IS NULL
                     AND e2.filtered = 0
                     AND s2.muted = 0
                     AND e2.subscription_id != entries.subscription_id
                 )"""
        )
        self._conn.commit()

    def record_deliveries(
        self,
        entry_ids: list[int],
        *,
        when: str | None = None,
        refresh: bool = False,
    ) -> int:
        """Remember which entries a digest handed out.

        First delivery wins by default (`ON CONFLICT DO NOTHING`), so replaying
        an old digest does not make an entry look new. `refresh=True` moves the
        stamp forward and is used only by the replay path: without it a replayed
        entry stays older than the window and is re-offered forever.
        """
        if not entry_ids:
            return 0
        self.begin_batch()
        stamp = when or _utc_now()
        conflict = (
            "DO UPDATE SET delivered_at = excluded.delivered_at"
            if refresh
            else "DO NOTHING"
        )
        changed = 0
        for entry_id in dict.fromkeys(entry_ids):
            cursor = self._conn.execute(
                "INSERT INTO digest_deliveries (entry_id, delivered_at) VALUES (?, ?)"
                f" ON CONFLICT(entry_id) {conflict}",
                (entry_id, stamp),
            )
            changed += max(cursor.rowcount, 0)
        self._conn.commit()
        return changed

    def delivery_times(self, entry_ids: list[int]) -> dict[int, str]:
        """delivered_at for the given ids that were already delivered."""
        if not entry_ids:
            return {}
        found: dict[int, str] = {}
        unique = list(dict.fromkeys(entry_ids))
        # Chunked to stay well inside SQLite's variable limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT entry_id, delivered_at FROM digest_deliveries"
                f" WHERE entry_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                found[int(row["entry_id"])] = row["delivered_at"]
        return found

    def delivered_since(self, since: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM digest_deliveries WHERE delivered_at >= ?", (since,)
        ).fetchone()
        return int(row["n"])

    def unread_counts(self, *, exclude_delivered: bool = False) -> dict[int, int]:
        """Visible unread entries per subscription, optionally skipping digests."""
        clauses = ["es.read_at IS NULL", "e.duplicate_of IS NULL", "e.filtered = 0"]
        if exclude_delivered:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM digest_deliveries AS d"
                " WHERE d.entry_id = e.id)"
            )
        return self._counts_by_subscription(clauses)

    def _counts_by_subscription(self, clauses: list[str]) -> dict[int, int]:
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            "SELECT e.subscription_id AS sid, COUNT(*) AS n"
            " FROM entries AS e"
            " LEFT JOIN entry_state AS es ON es.entry_id = e.id"
            f"{where}"
            " GROUP BY e.subscription_id",
            (),
        ).fetchall()
        return {int(row["sid"]): int(row["n"]) for row in rows}

    def begin_batch(self) -> None:
        """Batch a multi-row write (PostgreSQL autocommit connections).

        A no-op elsewhere; the method's own commit() closes the batch.
        """
        begin = getattr(self._conn, "begin_batch", None)
        if begin is not None:
            begin()

    def rebuild_duplicates(self) -> None:
        """Re-elect cross-feed keepers after a bulk load."""
        self._release_shadowed_keepers()
        self._conn.commit()
        self._reelect_keepers()

    @property
    def engine(self) -> str:
        return self._dialect.name

    def stats(self) -> dict[str, int]:
        """Library-wide counts in one statement.

        Feed health buckets are owned by `status.feed_health`, which applies a
        precedence (disabled beats failing beats muted) that a set of
        independent COUNTs cannot express; keeping them here too allowed the
        two to disagree.
        """
        row = self._conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM subscriptions) AS subscriptions,
                   (SELECT COUNT(*) FROM entries) AS entries,
                   (SELECT COUNT(*) FROM entries WHERE filtered = 1) AS entries_filtered,
                   (SELECT COUNT(*) FROM entries WHERE duplicate_of IS NOT NULL)
                       AS entries_duplicates,
                   (SELECT COUNT(*) FROM entry_state WHERE starred = 1) AS entries_starred,
                   (SELECT COUNT(*)
                      FROM entries AS e
                      LEFT JOIN entry_state AS es ON es.entry_id = e.id
                     WHERE es.read_at IS NULL
                       AND e.duplicate_of IS NULL
                       AND e.filtered = 0
                       AND NOT EXISTS (SELECT 1 FROM subscriptions AS ms
                                        WHERE ms.id = e.subscription_id AND ms.muted = 1))
                       AS entries_unread""",
            (),
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO server_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, value, _utc_now()),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT key, value, updated_at FROM server_meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return {"key": row["key"], "value": row["value"], "updated_at": row["updated_at"]}

    def record_prune(self, removed: int, *, when: str | None = None) -> dict[str, int | str]:
        """Persist the last prune run so a silent no-op becomes visible.

        Read-modify-write: correct for the single pruning writer this server
        ships (one process owns the loop). Replicas sharing one database would
        need an atomic increment.
        """
        total = 0
        stored = self.get_meta("prune")
        if stored and stored["value"]:
            try:
                total = int(json.loads(stored["value"]).get("total_removed") or 0)
            except (TypeError, ValueError, AttributeError):
                total = 0
        payload = {
            "last_run_at": when or _utc_now(),
            "last_removed": removed,
            "total_removed": total + removed,
        }
        self.set_meta("prune", json.dumps(payload))
        return payload

    def add_subscription(
        self,
        url: str,
        title: str | None = None,
        tags: list[str] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        muted: bool = False,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        fetch_interval: int | None = None,
    ) -> Subscription:
        normalized = url.strip()
        self._conn.execute(
            f"""INSERT {self._dialect.insert_ignore} INTO subscriptions
                   (url, title, tags, created_at, user_agent, headers,
                    muted, include_keywords, exclude_keywords, fetch_interval)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?){self._dialect.conflict_nothing}""",
            (
                normalized,
                title,
                json.dumps(tags or []),
                _utc_now(),
                user_agent,
                json.dumps(headers) if headers else None,
                int(muted),
                json.dumps(_clean_keywords(include_keywords)),
                json.dumps(_clean_keywords(exclude_keywords)),
                _clean_interval(fetch_interval),
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
        if cursor.rowcount > 0:
            self._reelect_keepers()
        return cursor.rowcount > 0

    def list_subscriptions(self, tag: str | None = None, failing_only: bool = False) -> list[Subscription]:
        sql = _SUBSCRIPTION_SQL
        clauses: list[str] = []
        params: list[object] = []
        if tag is not None:
            clauses.append(self._dialect.tags_exists("s"))
            params.append(tag)
        if failing_only:
            clauses.append("(s.last_error IS NOT NULL OR s.disabled = 1)")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
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
        muted: bool | None = None,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        fetch_interval: int | None = None,
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
        if muted is not None:
            assignments.append("muted = ?")
            params.append(int(muted))
        if include_keywords is not None:
            assignments.append("include_keywords = ?")
            params.append(json.dumps(_clean_keywords(include_keywords)))
        if exclude_keywords is not None:
            assignments.append("exclude_keywords = ?")
            params.append(json.dumps(_clean_keywords(exclude_keywords)))
        if fetch_interval is not None:
            assignments.append("fetch_interval = ?")
            params.append(_clean_interval(fetch_interval))
        if not assignments:
            return False
        params.append(subscription_id)
        cursor = self._conn.execute(
            f"UPDATE subscriptions SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        rules_changed = include_keywords is not None or exclude_keywords is not None
        if cursor.rowcount > 0 and rules_changed:
            self.reevaluate_filters(subscription_id)
        elif cursor.rowcount > 0 and muted is not None:
            # Mute only changes which keepers readers can see, not entry flags.
            self._release_shadowed_keepers()
            self._conn.commit()
            self._reelect_keepers()
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
        item_count: int | None = None,
    ) -> None:
        """Record fetch metadata; item_count is the accepted item total of a
        parsed response. It is left untouched for 304s and errors, so
        "fetched fine but produced nothing" stays a real, distinct signal
        instead of being inferred from a row count retention can zero."""
        if item_count is None:
            self._conn.execute(
                """UPDATE subscriptions
                   SET etag = ?, last_modified = ?, last_fetched_at = ?, last_error = ?
                   WHERE id = ?""",
                (etag, last_modified, last_fetched_at, last_error, subscription_id),
            )
        else:
            self._conn.execute(
                """UPDATE subscriptions
                   SET etag = ?, last_modified = ?, last_fetched_at = ?, last_error = ?,
                       last_item_count = ?
                   WHERE id = ?""",
                (
                    etag,
                    last_modified,
                    last_fetched_at,
                    last_error,
                    int(item_count),
                    subscription_id,
                ),
            )
        self._conn.commit()

    def update_feed_hints(
        self,
        subscription_id: int,
        *,
        ttl: int | None,
        skip_hours: list[int],
        skip_days: list[str],
    ) -> None:
        # Called only on a parsed 200; a 304 keeps the previously stored hints.
        self._conn.execute(
            """UPDATE subscriptions
               SET ttl = ?, skip_hours = ?, skip_days = ?
               WHERE id = ?""",
            (ttl, json.dumps(skip_hours), json.dumps(skip_days), subscription_id),
        )
        self._conn.commit()

    def _find_duplicate_keeper(self, subscription_id: int, item: EntryInput, key: str | None) -> int | None:
        if key is None:
            return None
        anchor_dt = datetime.now(timezone.utc)
        if item.published_at:
            try:
                anchor_dt = datetime.strptime(item.published_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        window = timedelta(days=_DEDUP_WINDOW_DAYS)
        lo = (anchor_dt - window).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (anchor_dt + window).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self._conn.execute(
            """SELECT e.id FROM entries AS e
               JOIN subscriptions AS s ON s.id = e.subscription_id
               WHERE e.canonical_url = ?
                 AND e.subscription_id != ?
                 AND e.duplicate_of IS NULL
                 AND e.filtered = 0
                 AND s.muted = 0
                 AND COALESCE(e.published_at, e.fetched_at) BETWEEN ? AND ?
               ORDER BY e.id LIMIT 1""",
            (key, subscription_id, lo, hi),
        ).fetchone()
        return row["id"] if row is not None else None

    def reevaluate_filters(self, subscription_id: int) -> None:
        """Rescan a subscription's entries against its current keyword rules.

        Runs when the rules change so filtering is retroactive and reversible:
        clearing a rule immediately reveals previously hidden entries. Copies
        whose keeper became unreadable (filtered or muted) are released so
        keeper re-election can promote a visible copy.
        """
        sub = self.get_subscription(subscription_id)
        if sub is None:
            return
        rows = self._conn.execute(
            "SELECT id, title, summary FROM entries WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchall()
        if rows:
            updates = [
                (
                    int(
                        _entry_filtered(
                            sub.include_keywords, sub.exclude_keywords,
                            row["title"], row["summary"],
                        )
                    ),
                    row["id"],
                )
                for row in rows
            ]
            self._conn.executemany("UPDATE entries SET filtered = ? WHERE id = ?", updates)
        self._release_shadowed_keepers()
        self._conn.commit()
        self._reelect_keepers()

    def upsert_entries(
        self,
        subscription_id: int,
        items: list[EntryInput],
        *,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
    ) -> int:
        self.begin_batch()
        written = 0
        flipped = False
        neq = self._dialect.null_safe_neq
        include = _clean_keywords(include_keywords)
        exclude = _clean_keywords(exclude_keywords)
        prepared = [
            (
                item,
                _entry_guid(item),
                canonical_key(item.url, item.title),
                int(_entry_filtered(include, exclude, item.title, item.summary)),
            )
            for item in items
        ]
        prior_flags = self._current_filtered_flags(subscription_id, [guid for _, guid, _, _ in prepared])
        for item, guid, key, filtered in prepared:
            duplicate_of = self._find_duplicate_keeper(subscription_id, item, key)
            cursor = self._conn.execute(
                f"""INSERT INTO entries (
                       subscription_id, guid, url, title, author,
                       published_at, summary, content, fetched_at, enclosures,
                       canonical_url, duplicate_of, filtered
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subscription_id, guid) DO UPDATE SET
                       url = excluded.url,
                       title = excluded.title,
                       author = excluded.author,
                       published_at = excluded.published_at,
                       summary = excluded.summary,
                       content = COALESCE(excluded.content, entries.content),
                       fetched_at = excluded.fetched_at,
                       enclosures = COALESCE(excluded.enclosures, entries.enclosures),
                       canonical_url = excluded.canonical_url,
                       duplicate_of = COALESCE(excluded.duplicate_of, entries.duplicate_of),
                       filtered = excluded.filtered
                   WHERE entries.url {neq} excluded.url
                      OR entries.title {neq} excluded.title
                      OR entries.author {neq} excluded.author
                      OR entries.published_at {neq} excluded.published_at
                      OR entries.summary {neq} excluded.summary
                      OR (
                          excluded.content IS NOT NULL
                          AND entries.content {neq} excluded.content
                      )
                      OR (
                          excluded.enclosures IS NOT NULL
                          AND entries.enclosures {neq} excluded.enclosures
                      )""",
                (
                    subscription_id,
                    guid,
                    _normalized_url(item.url),
                    item.title,
                    item.author,
                    item.published_at,
                    item.summary,
                    item.content,
                    _utc_now(),
                    json.dumps(item.enclosures) if item.enclosures else None,
                    key,
                    duplicate_of,
                    filtered,
                ),
            )
            if cursor.rowcount > 0:
                written += cursor.rowcount
                if guid in prior_flags and prior_flags[guid] != filtered:
                    flipped = True
        self._conn.commit()
        if flipped:
            # A content change can flip an existing keeper's filtered flag
            # (filtered = excluded.filtered above); converge keeper links right
            # away instead of leaving shadowed keepers until the next heal.
            # Re-election is deliberately window-free (see _reelect_keepers),
            # so it runs only when a flag actually flipped, never eagerly.
            self._release_shadowed_keepers()
            self._conn.commit()
            self._reelect_keepers()
        return written

    def _current_filtered_flags(self, subscription_id: int, guids: list[str]) -> dict[str, int]:
        if not guids:
            return {}
        placeholders = ",".join("?" * len(guids))
        rows = self._conn.execute(
            f"""SELECT guid, filtered FROM entries
                WHERE subscription_id = ? AND guid IN ({placeholders})""",
            [subscription_id, *guids],
        ).fetchall()
        return {row["guid"]: int(row["filtered"]) for row in rows}

    def _tag_exists_clause(self) -> str:
        return (
            "EXISTS (SELECT 1 FROM subscriptions AS ts"
            f" WHERE ts.id = e.subscription_id AND {self._dialect.tags_exists('ts')})"
        )

    def list_entries(
        self,
        *,
        subscription_id: int | None = None,
        unread_only: bool = False,
        limit: int = 20,
        since: str | None = None,
        include_duplicates: bool = False,
        tag: str | None = None,
        starred: bool | None = None,
        hide_delivered_since: str | None = None,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_duplicates:
            clauses.append("e.duplicate_of IS NULL")
        # Keyword-filtered entries stay hidden everywhere, even for an explicit
        # subscription; muted subscriptions only drop out of aggregate views.
        clauses.append("e.filtered = 0")
        if subscription_id is not None:
            clauses.append("e.subscription_id = ?")
            params.append(subscription_id)
        else:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM subscriptions AS ms"
                " WHERE ms.id = e.subscription_id AND ms.muted = 1)"
            )
        if tag is not None:
            clauses.append(self._tag_exists_clause())
            params.append(tag)
        if starred is not None:
            clauses.append("es.starred = 1" if starred else "COALESCE(es.starred, 0) = 0")
        if unread_only:
            clauses.append("es.read_at IS NULL")
        if since is not None:
            clauses.append("e.published_at >= ?")
            params.append(since)
        if hide_delivered_since is not None:
            # Filter in SQL, not after the LIMIT: skipping delivered rows in
            # Python would make a per-feed window of N return nothing once N
            # entries were already handed out. An empty cutoff means "hide
            # every delivery"; a timestamp hides only deliveries newer than it.
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM digest_deliveries AS dd"
                " WHERE dd.entry_id = e.id AND dd.delivered_at > ?)"
            )
            params.append(hide_delivered_since)
        params.append(_clamp_limit(limit))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""SELECT {_ENTRY_COLUMNS}
                {_ENTRY_FROM}
                {where}
                ORDER BY e.published_at DESC NULLS LAST, e.id DESC
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
        self.begin_batch()
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
        include_duplicates: bool = False,
        tag: str | None = None,
        since: str | None = None,
    ) -> list[Entry]:
        extra_clauses: list[str] = []
        extra_params: list[Any] = []
        if not include_duplicates:
            extra_clauses.append("e.duplicate_of IS NULL")
        extra_clauses.append("e.filtered = 0")
        if subscription_id is not None:
            extra_clauses.append("e.subscription_id = ?")
            extra_params.append(subscription_id)
        else:
            extra_clauses.append(
                "NOT EXISTS (SELECT 1 FROM subscriptions AS ms"
                " WHERE ms.id = e.subscription_id AND ms.muted = 1)"
            )
        if tag is not None:
            extra_clauses.append(self._tag_exists_clause())
            extra_params.append(tag)
        if since is not None:
            extra_clauses.append("e.published_at >= ?")
            extra_params.append(since)
        if unread_only:
            extra_clauses.append("es.read_at IS NULL")
        built = self._dialect.search_sql(
            _ENTRY_COLUMNS, query, extra_clauses, extra_params, _clamp_limit(limit)
        )
        if built is None:
            return []
        sql, params = built
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except self._dialect.search_errors:
            return []
        return [_entry_from_row(row) for row in rows]

    def mark_subscription_entries(self, subscription_id: int, read: bool) -> int:
        return self._mark_sweep(
            ["e.subscription_id = ?"], [subscription_id], read=read
        )

    def mark_entries_bulk(
        self,
        *,
        tag: str | None = None,
        before: str | None = None,
        read: bool = True,
    ) -> int:
        """Sweep-mark entries read (or unread) without enumerating ids.

        Scope: a tag, entries published before a timestamp, or everything.
        Sweeps never touch starred entries — a star means "keep this for
        later", and catch-up must not consume it. Explicit id-based
        mark_entries remains the way to change starred entries.
        """
        self.begin_batch()
        clauses: list[str] = []
        params: list[object] = []
        if tag is not None:
            clauses.append(self._dialect.tags_exists("s"))
            params.append(tag)
        if before is not None:
            clauses.append("COALESCE(e.published_at, e.fetched_at) < ?")
            params.append(before)
        return self._mark_sweep(clauses, params, read=read)

    def _mark_sweep(self, clauses: list[str], params: list[object], *, read: bool) -> int:
        read_at = _utc_now() if read else None
        read_filter = "es.read_at IS NULL" if read else "es.read_at IS NOT NULL"
        extra = f" AND {' AND '.join(clauses)}" if clauses else ""
        cursor = self._conn.execute(
            f"""INSERT INTO entry_state (entry_id, read_at)
                SELECT e.id, ?
                FROM entries AS e
                JOIN subscriptions AS s ON s.id = e.subscription_id
                LEFT JOIN entry_state AS es ON es.entry_id = e.id
                WHERE {read_filter} AND COALESCE(es.starred, 0) = 0{extra}
                ON CONFLICT(entry_id) DO UPDATE SET read_at = excluded.read_at""",
            [read_at, *params],
        )
        self._conn.commit()
        return cursor.rowcount

    def set_starred(self, entry_ids: list[int], starred: bool) -> int:
        self.begin_batch()
        changed = 0
        flag = int(starred)
        seen: set[int] = set()
        for entry_id in entry_ids:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            row = self._conn.execute(
                """SELECT e.id, es.starred
                   FROM entries AS e
                   LEFT JOIN entry_state AS es ON es.entry_id = e.id
                   WHERE e.id = ?""",
                (entry_id,),
            ).fetchone()
            if row is None or bool(row["starred"] or 0) == starred:
                continue
            self._conn.execute(
                """INSERT INTO entry_state (entry_id, starred)
                   VALUES (?, ?)
                   ON CONFLICT(entry_id) DO UPDATE SET starred = excluded.starred""",
                (entry_id, flag),
            )
            changed += 1
        self._conn.commit()
        return changed

    def prune_entries(self, cutoff_iso: str) -> int:
        cursor = self._conn.execute(
            """DELETE FROM entries
               WHERE COALESCE(published_at, fetched_at) < ?
                 AND id NOT IN (SELECT entry_id FROM entry_state WHERE starred = 1)""",
            (cutoff_iso,),
        )
        self._conn.commit()
        if cursor.rowcount > 0:
            self._reelect_keepers()
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