"""Prometheus text exposition for library state.

OTel span metrics (exported through Tempo) answer "how much traffic, how
fast, how many errors". They cannot answer "is anything actually stored and
working": counts, feed health, and whether the prune loop is alive are state,
not requests. This endpoint renders exactly that, from the same payload as
`server_status` and `rss-mcp status`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from rss_mcp import __version__
from rss_mcp.config import Settings
from rss_mcp.db import connect_db
from rss_mcp.status import feed_hosts, parse_timestamp, prune_state, status_payload
from rss_mcp.store import Store

logger = logging.getLogger("rss_mcp")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_UP_HELP = "1 when the database answered this scrape."


def _family(parts: list[str], name: str, kind: str, help_text: str) -> None:
    parts.append(f"# HELP {name} {help_text}")
    parts.append(f"# TYPE {name} {kind}")


def render_metrics(
    settings: Settings,
    store: Store,
    *,
    now: datetime | None = None,
) -> str:
    payload = status_payload(settings, store, now=now)
    parts: list[str] = []

    _family(parts, "rss_mcp_up", "gauge", _UP_HELP)
    parts.append("rss_mcp_up 1")

    _family(parts, "rss_mcp_info", "gauge", "Build information; value is always 1.")
    parts.append(
        f'rss_mcp_info{{version="{__version__}",engine="{payload["database"]["engine"]}"}} 1'
    )

    _family(parts, "rss_mcp_subscriptions", "gauge", "Subscriptions by health state.")
    for state in ("ok", "failing", "disabled", "muted", "never_fetched"):
        parts.append(f'rss_mcp_subscriptions{{state="{state}"}} {payload["feeds"][state]}')
    # An overlay, not a sixth exclusive bucket: an empty feed is still "ok" in
    # the family above, so summing it must keep matching the subscription count.
    _family(
        parts,
        "rss_mcp_feeds_empty",
        "gauge",
        "Feeds whose last parsed fetch accepted zero items.",
    )
    parts.append(f"rss_mcp_feeds_empty {payload['feeds']['empty']}")

    reading = payload["reading"]
    _family(
        parts,
        "rss_mcp_digest_pending",
        "gauge",
        "Visible unread entries a digest has not handed out yet.",
    )
    parts.append(f"rss_mcp_digest_pending {reading['pending']}")
    _family(
        parts,
        "rss_mcp_digest_delivered_24h",
        "gauge",
        "Entries delivered by get_digest in the last 24 hours.",
    )
    parts.append(f"rss_mcp_digest_delivered_24h {reading['delivered_24h']}")
    if reading["by_tag"]:
        _family(
            parts,
            "rss_mcp_tag_pending",
            "gauge",
            "Undelivered unread entries per tag.",
        )
        for tag, count in sorted(reading["by_tag"].items()):
            parts.append(f'rss_mcp_tag_pending{{tag="{_escape(tag)}"}} {count}')

    _family(parts, "rss_mcp_entries", "gauge", "Cached entries by state.")
    database = payload["database"]
    for state, key in (
        ("total", "entries"),
        ("unread", "unread"),
        ("starred", "starred"),
        ("filtered", "filtered"),
        ("duplicates_hidden", "duplicates_hidden"),
    ):
        parts.append(f'rss_mcp_entries{{state="{state}"}} {database[key]}')

    hosts = _host_rows(store)
    if hosts:
        _family(
            parts,
            "rss_mcp_feed_last_success_timestamp_seconds",
            "gauge",
            "Unix time of the most recent successful fetch per host.",
        )
        for host, row in sorted(hosts.items()):
            if row["last_success"]:
                parts.append(
                    f'rss_mcp_feed_last_success_timestamp_seconds{{host="{host}"}} {row["last_success"]}'
                )
        _family(
            parts,
            "rss_mcp_feed_consecutive_failures",
            "gauge",
            "Sum of consecutive fetch failures across feeds per host.",
        )
        for host, row in sorted(hosts.items()):
            parts.append(f'rss_mcp_feed_consecutive_failures{{host="{host}"}} {row["failures"]}')

    last_success = parse_timestamp(payload["fetching"]["last_successful_fetch_at"])
    if last_success is not None:
        _family(
            parts,
            "rss_mcp_last_successful_fetch_timestamp_seconds",
            "gauge",
            "Unix time of the most recent successful feed fetch.",
        )
        parts.append(
            f"rss_mcp_last_successful_fetch_timestamp_seconds {int(last_success.timestamp())}"
        )

    _family(
        parts,
        "rss_mcp_refresh_interval_seconds",
        "gauge",
        "In-process refresh interval; 0 when fetching is driven externally.",
    )
    parts.append(f"rss_mcp_refresh_interval_seconds {settings.refresh_interval or 0}")

    retention = payload["retention"]
    prune = prune_state(store)
    last_prune = parse_timestamp(retention["last_prune_at"])
    if last_prune is not None:
        _family(
            parts,
            "rss_mcp_prune_last_run_timestamp_seconds",
            "gauge",
            "Unix time of the last retention prune cycle (including no-ops).",
        )
        parts.append(f"rss_mcp_prune_last_run_timestamp_seconds {int(last_prune.timestamp())}")
    if prune.get("total_removed") is not None:
        _family(
            parts,
            "rss_mcp_prune_removed_total",
            "counter",
            "Entries deleted by retention pruning since the counter started.",
        )
        parts.append(f"rss_mcp_prune_removed_total {int(prune['total_removed'])}")

    db_health = payload["database_health"]
    if db_health is not None:
        for name, help_text in (
            ("rss_mcp_db_idle_in_transaction", "Connections idle inside a transaction."),
            ("rss_mcp_db_lock_waiters", "Sessions currently waiting on a lock."),
            ("rss_mcp_db_connections", "Connections to this database."),
        ):
            _family(parts, name, "gauge", help_text)
        parts.append(f"rss_mcp_db_idle_in_transaction {db_health['idle_in_transaction']}")
        parts.append(f"rss_mcp_db_lock_waiters {db_health['lock_waiters']}")
        parts.append(f"rss_mcp_db_connections {db_health['connections']}")

    _family(
        parts,
        "rss_mcp_retention_days",
        "gauge",
        "Configured retention window; 0 when retention is disabled.",
    )
    parts.append(f"rss_mcp_retention_days {retention['retention_days'] or 0}")

    _family(
        parts,
        "rss_mcp_status_warnings",
        "gauge",
        "Count of self-reported problems from server_status.",
    )
    parts.append(f"rss_mcp_status_warnings {len(payload['warnings'])}")

    return "\n".join(parts) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _host_rows(store: Store) -> dict[str, dict[str, int]]:
    return feed_hosts(store.list_subscriptions())


def render_from_settings(settings: Settings, *, now: datetime | None = None) -> str:
    """Open a dedicated connection so a scrape never shares a transaction."""
    conn = None
    try:
        conn = connect_db(settings, autocommit=True)
        # No schema bootstrap and no dedup maintenance here: a scrape must be
        # cheap and read-only, and the server has healed the library at startup.
        return render_metrics(settings, Store(conn, maintain=False), now=now)
    except Exception:
        logger.exception("metrics scrape failed")
        parts = ["# HELP rss_mcp_up " + _UP_HELP, "# TYPE rss_mcp_up gauge", "rss_mcp_up 0"]
        return "\n".join(parts) + "\n"
    finally:
        if conn is not None:
            conn.close()
