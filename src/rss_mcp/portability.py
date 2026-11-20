"""Portable JSONL export and import.

Two jobs, one format: backups that do not depend on `pg_dump` or the SQLite
file, and moving a library between engines. Rows are streamed, ids are
re-resolved on import (URL for subscriptions, guid per subscription for
entries), and cross-feed duplicate links are remapped in a second pass.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from rss_mcp.dialect import dialect_for
from rss_mcp.store import Store

FORMAT = "rss-mcp-export"
FORMAT_VERSION = 1

_SUBSCRIPTION_COLUMNS = (
    "id",
    "url",
    "title",
    "tags",
    "etag",
    "last_modified",
    "last_fetched_at",
    "last_error",
    "created_at",
    "user_agent",
    "headers",
    "failure_count",
    "disabled",
    "muted",
    "include_keywords",
    "exclude_keywords",
    "fetch_interval",
    "ttl",
    "skip_hours",
    "skip_days",
)

_ENTRY_COLUMNS = (
    "e.id",
    "e.subscription_id",
    "e.guid",
    "e.url",
    "e.title",
    "e.author",
    "e.published_at",
    "e.summary",
    "e.content",
    "e.fetched_at",
    "e.enclosures",
    "e.canonical_url",
    "e.duplicate_of",
    "e.filtered",
    "es.read_at",
    "es.starred",
    "dd.delivered_at",
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _batch(conn: Any, sql: str, params: Iterable[Any] = (), size: int = 500) -> Iterator[Any]:
    cursor = conn.execute(sql, tuple(params))
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        for row in rows:
            yield row


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def export_lines(
    conn: Any,
    *,
    since: str | None = None,
    include_content: bool = True,
) -> Iterator[str]:
    """Yield one JSON object per line: a header, subscriptions, then entries."""
    yield json.dumps(
        {
            "type": FORMAT,
            "version": FORMAT_VERSION,
            "engine": dialect_for(conn).name,
            "since": since,
        }
    )
    for row in _batch(conn, f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions ORDER BY id"):
        payload = _row_dict(row)
        payload["type"] = "subscription"
        yield json.dumps(payload, ensure_ascii=False)

    where = ""
    params: list[Any] = []
    if since:
        # Same rule the store applies elsewhere: an entry with no pubDate is
        # dated by when it was fetched, so a --since backup cannot silently
        # drop undated items.
        where = " WHERE COALESCE(e.published_at, e.fetched_at) >= ?"
        params.append(since)
    sql = (
        f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM entries AS e"
        " LEFT JOIN entry_state AS es ON es.entry_id = e.id"
        " LEFT JOIN digest_deliveries AS dd ON dd.entry_id = e.id"
        f"{where} ORDER BY e.id"
    )
    for row in _batch(conn, sql, params):
        payload = _row_dict(row)
        payload["type"] = "entry"
        # subscription_id and duplicate_of are remapped by the importer.
        if not include_content:
            payload["content"] = None
        yield json.dumps(payload, ensure_ascii=False)


_SUBSCRIPTION_DEFAULTS: dict[str, Any] = {
    "created_at": "",
    "failure_count": 0,
    "disabled": 0,
    "muted": 0,
}


def _set_delivery(conn: Any, entry_id: int, delivered_at: str | None, *, restore: bool) -> bool:
    if not delivered_at:
        return False
    if restore:
        conn.execute(
            "INSERT INTO digest_deliveries (entry_id, delivered_at) VALUES (?, ?)"
            " ON CONFLICT(entry_id) DO UPDATE SET delivered_at = excluded.delivered_at",
            (entry_id, delivered_at),
        )
        return True
    # Merge: the backup is older, so it may add a delivery but never move one
    # backwards, which would re-offer entries the caller has already seen.
    conn.execute(
        "INSERT INTO digest_deliveries (entry_id, delivered_at) VALUES (?, ?)"
        " ON CONFLICT(entry_id) DO NOTHING",
        (entry_id, delivered_at),
    )
    return True


def _insert_subscription(conn: Any, record: dict[str, Any]) -> int:
    columns = [name for name in _SUBSCRIPTION_COLUMNS if name != "id"]
    placeholders = ", ".join("?" for _ in columns)
    now = record.get("created_at") or _utc_stamp()
    values = [
        now if name == "created_at" else record.get(name, _SUBSCRIPTION_DEFAULTS.get(name))
        for name in columns
    ]
    conn.execute(
        f"INSERT INTO subscriptions ({', '.join(columns)}) VALUES ({placeholders})", values
    )
    row = conn.execute(
        "SELECT id FROM subscriptions WHERE url = ?", (record["url"],)
    ).fetchone()
    return int(row["id"])


def _insert_entry(conn: Any, record: dict[str, Any], subscription_id: int) -> int:
    columns = [
        "subscription_id",
        "guid",
        "url",
        "title",
        "author",
        "published_at",
        "summary",
        "content",
        "fetched_at",
        "enclosures",
        "canonical_url",
        "filtered",
    ]
    placeholders = ", ".join("?" for _ in columns)
    values = [subscription_id] + [
        int(record.get("filtered") or 0) if name == "filtered" else record.get(name)
        for name in columns[1:]
    ]
    conn.execute(f"INSERT INTO entries ({', '.join(columns)}) VALUES ({placeholders})", values)
    row = conn.execute(
        "SELECT id FROM entries WHERE subscription_id = ? AND guid = ?",
        (subscription_id, record["guid"]),
    ).fetchone()
    return int(row["id"])


def _set_state(
    conn: Any,
    entry_id: int,
    read_at: str | None,
    starred: int | None,
    *,
    restore: bool,
) -> None:
    if read_at is None and not starred:
        return
    if restore:
        conn.execute(
            "INSERT INTO entry_state (entry_id, read_at, starred) VALUES (?, ?, ?)"
            " ON CONFLICT(entry_id) DO UPDATE SET read_at = excluded.read_at,"
            " starred = excluded.starred",
            (entry_id, read_at, int(starred or 0)),
        )
        return
    # Merge: a backup is older than the library it is merged into, so it may
    # only add state, never clear it. MAX/CASE keeps this portable across
    # SQLite and PostgreSQL (no GREATEST on SQLite).
    conn.execute(
        "INSERT INTO entry_state (entry_id, read_at, starred) VALUES (?, ?, ?)"
        " ON CONFLICT(entry_id) DO UPDATE SET"
        " read_at = COALESCE(entry_state.read_at, excluded.read_at),"
        " starred = CASE WHEN entry_state.starred = 1 OR excluded.starred = 1"
        " THEN 1 ELSE 0 END",
        (entry_id, read_at, int(starred or 0)),
    )


_REQUIRED = {
    "subscription": ("id", "url"),
    "entry": ("id", "subscription_id", "guid", "fetched_at"),
}


def _require(record: dict[str, Any], kind: str, number: int) -> None:
    for field in _REQUIRED[kind]:
        if record.get(field) is None:
            raise ValueError(f"line {number}: {kind} record is missing required field {field!r}")


def import_lines(conn: Any, lines: Iterable[str], *, replace: bool = False) -> dict[str, int]:
    """Load an export into `conn`; returns a per-category summary."""
    reader = iter(lines)
    try:
        header = json.loads(next(reader))
    except StopIteration:
        raise ValueError("empty import: expected an rss-mcp export header") from None
    if not isinstance(header, dict) or header.get("type") != FORMAT:
        raise ValueError("not an rss-mcp export: the first line must be the format header")
    if int(header.get("version") or 0) > FORMAT_VERSION:
        raise ValueError(
            f"export format {header.get('version')} is newer than this server supports "
            f"({FORMAT_VERSION})"
        )

    if replace:
        # Cascades to entries and entry_state on both engines. Deliberately
        # NOT committed here: the wipe must fail atomically with the load, or
        # one bad line in the backup leaves an empty library.
        conn.execute("DELETE FROM subscriptions")

    summary = {
        "subscriptions_added": 0,
        "subscriptions_matched": 0,
        "entries_added": 0,
        "entries_skipped": 0,
        "states_restored": 0,
        "deliveries_restored": 0,
        "duplicates_relinked": 0,
    }
    subscription_ids: dict[int, int] = {}
    entry_ids: dict[int, int] = {}
    duplicate_pairs: list[tuple[int, int]] = []

    for number, line in enumerate(reader, start=2):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number}: invalid JSON ({exc.msg})") from None
        kind = record.get("type")
        if kind == "subscription":
            _require(record, "subscription", number)
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE url = ?", (record["url"],)
            ).fetchone()
            if existing is not None:
                subscription_ids[int(record["id"])] = int(existing["id"])
                summary["subscriptions_matched"] += 1
                continue
            subscription_ids[int(record["id"])] = _insert_subscription(conn, record)
            summary["subscriptions_added"] += 1
        elif kind == "entry":
            _require(record, "entry", number)
            original_subscription = int(record["subscription_id"])
            if original_subscription not in subscription_ids:
                raise ValueError(
                    f"line {number}: entry references subscription {original_subscription} "
                    "that was not exported before it"
                )
            subscription_id = subscription_ids[original_subscription]
            existing = conn.execute(
                "SELECT id FROM entries WHERE subscription_id = ? AND guid = ?",
                (subscription_id, record["guid"]),
            ).fetchone()
            if existing is not None:
                entry_ids[int(record["id"])] = int(existing["id"])
                summary["entries_skipped"] += 1
            else:
                entry_ids[int(record["id"])] = _insert_entry(conn, record, subscription_id)
                summary["entries_added"] += 1
            if record.get("read_at") or record.get("starred"):
                _set_state(
                    conn,
                    entry_ids[int(record["id"])],
                    record.get("read_at"),
                    record.get("starred"),
                    restore=replace,
                )
                summary["states_restored"] += 1
            if _set_delivery(
                conn,
                entry_ids[int(record["id"])],
                record.get("delivered_at"),
                restore=replace,
            ):
                summary["deliveries_restored"] += 1
            if record.get("duplicate_of") is not None:
                duplicate_pairs.append(
                    (entry_ids[int(record["id"])], int(record["duplicate_of"]))
                )
        else:
            raise ValueError(f"line {number}: unknown record type {kind!r}")

    # Second pass: duplicate_of points at exported ids, which no longer exist.
    for entry_id, original_keeper in duplicate_pairs:
        keeper = entry_ids.get(original_keeper)
        if keeper is None or keeper == entry_id:
            continue
        conn.execute("UPDATE entries SET duplicate_of = ? WHERE id = ?", (keeper, entry_id))
        summary["duplicates_relinked"] += 1
    conn.commit()
    # A filtered export can lose the keeper a copy pointed at, and an old
    # export may predate canonical_url; re-elect from canonical_url so the
    # restored library shows one copy per article.
    Store(conn, maintain=False).rebuild_duplicates()
    return summary
