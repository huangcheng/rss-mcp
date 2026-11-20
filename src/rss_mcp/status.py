"""Server self-inspection.

One payload feeds the `server_status` tool, `rss-mcp status`, and `/metrics`,
so the three surfaces can never disagree about what the server thinks of
itself. Everything here is derived from the database except the configured
settings, which means it stays truthful when the fetch driver is external
(Temporal) rather than the in-process loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from rss_mcp import __version__
from rss_mcp.config import Settings
from rss_mcp.models import Subscription
from rss_mcp.store import Store

# The prune loop wakes hourly when retention is configured; three missed
# cycles is a real signal (the 0.3.5 silent no-op went unnoticed for 14h).
PRUNE_STALE_SECONDS = 3 * 3600
# A reading library that has not completed a single fetch in a day is stuck,
# whatever the driver is supposed to be doing.
FETCH_STALE_SECONDS = 24 * 3600

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_TS_FORMAT)


def feed_health(subscriptions: list[Subscription]) -> dict[str, int]:
    """Mutually exclusive feed buckets; every feed lands in exactly one."""
    buckets = {"ok": 0, "failing": 0, "disabled": 0, "muted": 0, "never_fetched": 0}
    for sub in subscriptions:
        if sub.disabled:
            buckets["disabled"] += 1
        elif sub.last_error:
            buckets["failing"] += 1
        elif sub.muted:
            buckets["muted"] += 1
        elif not sub.last_fetched_at:
            buckets["never_fetched"] += 1
        else:
            buckets["ok"] += 1
    return buckets


def feed_hosts(subscriptions: list[Subscription]) -> dict[str, dict[str, int]]:
    """Per-host rollup for metrics labels (hostname, not the full URL)."""
    hosts: dict[str, dict[str, int]] = {}
    for sub in subscriptions:
        host = urlsplit(sub.url).hostname or sub.url
        row = hosts.setdefault(
            host, {"feeds": 0, "failing": 0, "failures": 0, "last_success": 0}
        )
        row["feeds"] += 1
        if sub.last_error:
            row["failing"] += 1
            row["failures"] += sub.failure_count
        else:
            stamp = parse_timestamp(sub.last_fetched_at)
            if stamp is not None:
                row["last_success"] = max(row["last_success"], int(stamp.timestamp()))
    return hosts


def prune_state(store: Store) -> dict[str, Any]:
    record = store.get_meta("prune")
    if not record or not record["value"]:
        return {}
    try:
        parsed = json.loads(record["value"])
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _age_seconds(now: datetime, value: Any) -> int | None:
    stamp = parse_timestamp(value if isinstance(value, str) else None)
    if stamp is None:
        return None
    return int((now - stamp).total_seconds())


def _warnings(
    settings: Settings,
    subscriptions: list[Subscription],
    health: dict[str, int],
    prune: dict[str, Any],
    last_success: str | None,
    now: datetime,
) -> list[str]:
    warnings: list[str] = []
    if settings.retention_days:
        if not prune:
            warnings.append(
                "retention is configured but pruning has never recorded a run; "
                "the prune loop may not be running"
            )
        else:
            age = _age_seconds(now, prune.get("last_run_at"))
            if age is None:
                warnings.append("retention is configured but the recorded prune run is unreadable")
            elif age > PRUNE_STALE_SECONDS:
                warnings.append(
                    f"retention is configured but the last prune ran {age // 3600}h ago"
                )
    if health.get("total"):
        if last_success is None:
            # No feed has ever fetched: the very stall this check exists for.
            oldest = min(
                (
                    parse_timestamp(sub.created_at)
                    for sub in subscriptions
                    if sub.created_at
                ),
                default=None,
            )
            age = int((now - oldest).total_seconds()) if oldest else None
            if age is None or age > FETCH_STALE_SECONDS:
                warnings.append(
                    "no feed has fetched successfully yet; the fetch driver may not be running"
                )
        else:
            age = _age_seconds(now, last_success)
            if age is not None and age > FETCH_STALE_SECONDS:
                warnings.append(f"no feed has fetched successfully in {age // 3600}h")
    if health["failing"]:
        warnings.append(
            f"{health['failing']} feed(s) failing; see list_subscriptions(failing_only=true)"
        )
    if health.get("empty"):
        warnings.append(
            f"{health['empty']} feed(s) fetch successfully but store no entries; "
            "the feed may be empty upstream or parse to nothing"
        )
    return warnings


def reading_state(store: Store, subscriptions: list[Subscription], *, now: datetime) -> dict[str, Any]:
    """What has been handed out, and what is still waiting, per tag."""
    visible = {sub.id for sub in subscriptions if not sub.muted}
    pending_by_subscription = store.unread_counts(exclude_delivered=True)
    pending = sum(n for sid, n in pending_by_subscription.items() if sid in visible)
    by_tag: dict[str, int] = {}
    for sub in subscriptions:
        # Same visibility as `pending` above and get_digest's feed selection:
        # muted feeds are hidden, disabled ones keep their entries readable.
        if sub.muted:
            continue
        count = pending_by_subscription.get(sub.id, 0)
        if not count:
            continue
        for tag in sub.tags or []:
            by_tag[tag] = by_tag.get(tag, 0) + count
    delivered_24h = store.delivered_since(
        (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {
        "pending": pending,
        "delivered_24h": delivered_24h,
        "by_tag": dict(sorted(by_tag.items(), key=lambda item: -item[1])),
    }


def status_payload(
    settings: Settings,
    store: Store,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    counts = store.stats()
    subscriptions = store.list_subscriptions()
    health = feed_health(subscriptions)
    health["total"] = len(subscriptions)
    # "Empty" means the last parsed fetch accepted zero items. Counting cached
    # rows instead would flag healthy low-volume feeds as soon as retention
    # pruned their only entry.
    health["empty"] = sum(
        1
        for sub in subscriptions
        if not sub.disabled
        and not sub.last_error
        and sub.last_fetched_at
        and sub.last_item_count == 0
    )
    prune = prune_state(store)
    successes = [sub.last_fetched_at for sub in subscriptions if not sub.last_error]
    last_success = max((stamp for stamp in successes if stamp), default=None)
    failing = [
        {
            "subscription_id": sub.id,
            "title": sub.title,
            "host": urlsplit(sub.url).hostname or sub.url,
            "last_error": sub.last_error,
            "failure_count": sub.failure_count,
            "disabled": sub.disabled,
        }
        for sub in subscriptions
        if sub.last_error or sub.disabled
    ]
    # Auto-disabled first (they carry an error), then by failure count.
    failing.sort(key=lambda row: (not row["last_error"], -(row["failure_count"] or 0)))
    return {
        "version": __version__,
        "generated_at": format_timestamp(now),
        "database": {
            "engine": store.engine,
            "subscriptions": counts["subscriptions"],
            "entries": counts["entries"],
            "unread": counts["entries_unread"],
            "starred": counts["entries_starred"],
            "filtered": counts["entries_filtered"],
            "duplicates_hidden": counts["entries_duplicates"],
        },
        "feeds": health,
        "fetching": {
            "driver": "in-process" if settings.refresh_interval else "external-or-manual",
            "refresh_interval_seconds": settings.refresh_interval,
            "concurrency": settings.fetch_concurrency,
            "disable_after_failures": settings.disable_after_failures,
            "last_successful_fetch_at": last_success,
            "seconds_since_last_success": _age_seconds(now, last_success),
        },
        "retention": {
            "retention_days": settings.retention_days,
            "last_prune_at": prune.get("last_run_at"),
            "last_prune_removed": prune.get("last_removed"),
            "prune_total_removed": prune.get("total_removed"),
            "seconds_since_last_prune": _age_seconds(now, prune.get("last_run_at")),
        },
        # Feeds that need attention, mirroring list_subscriptions(failing_only=true).
        "reading": reading_state(store, subscriptions, now=now),
        "failing_feeds": failing[:10],
        "warnings": _warnings(settings, subscriptions, health, prune, last_success, now),
    }
