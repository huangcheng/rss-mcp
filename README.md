# rss-mcp

Agent-only MCP server for RSS and Atom. A host drives it over stdio (default) or Streamable HTTP. There is no GUI, TUI, or interactive CLI. Subscriptions, cached entries, unread state, and full-text search live in one local SQLite file. Fifteen tools, two read-only resources, one prompt, and live update notifications; no MCP Apps.

Protocol: [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) (stdio or Streamable HTTP; tools + resources) via the official Python SDK v2 (`mcp` ≥ 2.3).

Requires Python 3.11+. Install from [PyPI](https://pypi.org/project/rss-mcp-server/): `uv tool install rss-mcp-server` gives you the `rss-mcp` and `rss-mcp-server` commands, or run it once with `uvx rss-mcp-server`.

## Documentation

Full docs live in [`docs/`](docs/):

- [Tool reference](docs/tools.md) — every parameter and response field
- [Deployment](docs/deployment.md) — stdio, Docker, PostgreSQL, Temporal, backups
- [Observability](docs/observability.md) — logs, traces, `/metrics`, `server_status`
- [Stability policy](docs/stability.md) — what 1.0 freezes and how changes are deprecated

The frozen 1.0 surface is machine-readable in [`docs/freeze-manifest.json`](docs/freeze-manifest.json)
and enforced by `tests/test_freeze_manifest.py`.

## Host config

In stdio mode, the process reads JSON-RPC from stdin and writes MCP messages only to stdout. It exits when stdin closes.

Cursor `mcp.json` (any stdio host can use the same command):

```json
{
  "mcpServers": {
    "rss-mcp": {
      "command": "uvx",
      "args": ["rss-mcp-server"],
      "env": {
        "RSS_MCP_DB": "/absolute/path/rss.db"
      }
    }
  }
}
```

The PyPI distribution is `rss-mcp-server` (`rss-mcp` is blocked by a similar existing project); it installs both the `rss-mcp-server` and `rss-mcp` commands. From this checkout: `uv run rss-mcp`, or `uvx --from /absolute/path/to/rss-mcp rss-mcp`.

## HTTP transport

Run the same server over Streamable HTTP instead of stdio, straight from PyPI:

```bash
uvx rss-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

After `uv tool install rss-mcp-server`, use `rss-mcp --transport streamable-http …` instead; from this checkout, `uv run rss-mcp --transport streamable-http …`.

| Flag | Default |
| --- | --- |
| `--transport` | `stdio` (choices: `stdio`, `streamable-http`) |
| `--host` | `127.0.0.1` |
| `--port` | `8000` |
| `--path` | `/mcp` |

The MCP endpoint is then `http://127.0.0.1:8000/mcp`. Hosts that support remote servers can point at it directly:

```json
{
  "mcpServers": {
    "rss-mcp": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

All sessions share one SQLite database (same `RSS_MCP_DB` setting). The endpoint serves both protocol eras: 2026-07-28 stateless per-request POSTs (with the `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers and `_meta` envelope) and the legacy `initialize` handshake (up to 2025-11-25, with sessions) for older hosts.

Set `RSS_MCP_AUTH_TOKEN` (or `RSS_MCP_AUTH_TOKENS` for several comma-separated tokens) to require `Authorization: Bearer <token>` on every MCP request (both eras; stdio is unaffected). Hosts that support custom headers:

```json
{
  "mcpServers": {
    "rss-mcp": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Without a token the endpoint is unauthenticated: keep the default loopback bind, or put it behind a reverse proxy with auth before exposing it beyond localhost.

The HTTP app also serves an unauthenticated `GET /healthz` (JSON: status, version, subscription count) for monitoring, and applies a per-client sliding-window rate limit (`RSS_MCP_RATE_LIMIT_PER_MINUTE`, default 120, `0` disables; keyed on bearer token or client IP, replies `429` when exceeded).

### Prometheus metrics

Set `RSS_MCP_METRICS_TOKEN` to enable `GET /metrics` (Prometheus text format, bearer-protected with that token; the route answers `404` when the variable is unset):

```bash
curl -H "Authorization: Bearer $RSS_MCP_METRICS_TOKEN" http://127.0.0.1:8000/metrics
```

Metrics are state, not traffic — pair them with the OTel spans, which carry call counts and latency: `rss_mcp_subscriptions{state="ok|failing|disabled|muted|never_fetched"}` (mutually exclusive, so they sum to the subscription count), `rss_mcp_feeds_empty` (overlay: last fetch accepted zero items), `rss_mcp_entries{state="total|unread|starred|filtered|duplicates_hidden"}`, `rss_mcp_feed_last_success_timestamp_seconds{host}`, `rss_mcp_feed_consecutive_failures{host}`, `rss_mcp_prune_last_run_timestamp_seconds`, `rss_mcp_prune_removed_total`, `rss_mcp_retention_days`, `rss_mcp_digest_pending` (unread entries no digest has handed out yet), `rss_mcp_digest_delivered_24h`, `rss_mcp_tag_pending{tag}`, `rss_mcp_status_warnings` (alert on `> 0`), plus `rss_mcp_up` / `rss_mcp_info`. A scrape opens its own connection and never shares the request path's transaction.

### Backup, restore, and engine migration

`rss-mcp export-db` writes the whole library as JSON Lines (a header, then one object per subscription and per entry, including read/star state and cross-feed duplicate links); `rss-mcp import-db` reads it back. Both use the same engine-agnostic path, so `sqlite -> JSONL -> postgres` (or the reverse) is the documented migration:

```bash
rss-mcp export-db --out rss-backup.jsonl          # --out - streams to stdout
rss-mcp export-db --out small.jsonl --no-content  # omit article bodies, keep summaries
rss-mcp import-db --in rss-backup.jsonl           # merge: match subscriptions by URL, entries by guid
rss-mcp import-db --in rss-backup.jsonl --replace # wipe the target library first (destructive)
```

Import is idempotent: re-running the same file adds nothing. A merge only ever *adds* state — it never clears a read timestamp or a star you added after the backup — while `--replace` restores the file verbatim and runs as one transaction, so a truncated backup leaves the previous library untouched. `--since` compares an entry's publication date and falls back to when it was fetched, so undated items are not silently dropped. Subscriptions carry their `headers`, so treat an export as sensitive when feeds use per-feed auth headers.

### Self-inspection

`server_status` (tool) and `rss-mcp status` (CLI, prints the same JSON) report database counts, feed health (including feeds that store nothing), scheduling/retention configuration, the last prune run, a `reading` section (`pending`, `delivered_24h`, per-tag pending), feeds needing attention, and `warnings`. Warnings cover the silent-failure cases this server has actually hit: retention configured but pruning never ran (or went stale), no feed fetching successfully (including the never-fetched case, after a one-day grace period), and failing feeds. Deliberately paused feeds are not warnings.

## Server behavior

- **Concurrent fetching**: `fetch_feed` without an id refreshes all subscriptions concurrently (`RSS_MCP_FETCH_CONCURRENCY`, default 8).
- **Failure handling**: failed feeds record `last_error` and a failure count, then back off exponentially (60s doubling to 1h) in bulk fetches. After `RSS_MCP_DISABLE_AFTER_FAILURES` consecutive failures (default 5, `0` never) a feed is auto-disabled and skipped until fetched by id or re-enabled via `update_subscription`.
- **Scheduled refresh**: set `RSS_MCP_REFRESH_INTERVAL` (seconds) to refresh all feeds in the background on that interval; unset means fetch-on-demand only.
- **Retention**: set `RSS_MCP_RETENTION_DAYS` to prune entries older than that many days on each scheduled refresh (starred entries are kept). Unset keeps everything.
- **Per-feed overrides**: `add_subscription` / `update_subscription` accept `user_agent` and `headers` for feeds that block the default client.
- **Keyword filters**: `include_keywords` / `exclude_keywords` per subscription hide entries whose title or summary misses (or hits) a keyword — case-insensitive substring. Filtered entries are stored but flagged, hidden from listing, search, unread counts, and the unread resource. Rule edits re-apply to cached entries, so filtering is retroactive and reversible.
- **Mute**: `muted=true` keeps a feed fetched but hides it from aggregate views (listings/search without a `subscription_id`, unread counts). Explicit `subscription_id` queries still return its entries.
- **RSS 2.0 spec hints**: the feed's `ttl` (minutes) acts as its default refresh interval (an explicit `fetch_interval` overrides it), and `skipHours`/`skipDays` (GMT) pause bulk refreshes during the declared windows — honored by both the built-in loop and the Temporal scheduler. Items without a link fall back to a permalink `guid`; enclosure URLs must be http(s). Conditional requests (ETag/Last-Modified → 304) complement these hints at the HTTP layer.
- **Incremental digests**: `get_digest` records which entries it handed out, so the next call shows what is *new* instead of the same newest-N pile. `include_delivered=true` returns everything again, `replay_days=N` re-offers entries last delivered more than N days ago, and `mark_read=true` marks exactly the delivered entries read (starred entries are spared — a star means "keep for later"). The response carries `delivered`, `marked_read`, and `pending`.
- **Empty-feed detection**: a feed that fetches successfully but stores zero entries is reported in `server_status.feed_health`-adjacent `feeds.empty`, as a `warnings` entry, and as `rss_mcp_subscriptions{state="empty"}` — distinct from "failing", and invisible before.
- **OpenTelemetry traces** (`rss-mcp-server[otel]` + `RSS_MCP_OTEL_OTLP_ENDPOINT`): every MCP request is traced by the SDK middleware (gen_ai conventions, distributed trace-context), plus `rss.feed_fetch` spans (host, status, duration, 304 outcome) and `rss.refresh_all` round spans — point it at any OTLP/HTTP receiver (Tempo, collector) and read the waterfalls in Grafana. Off by default; zero overhead when unset.

## Live notifications

Hosts can stop polling: call `subscriptions/listen` with a `resource_subscriptions` filter (protocol revision 2026-07-28) and the server pushes `notifications/resources/updated` for `rss://entries/unread` and `rss://subscriptions` whenever a fetch lands new entries, read state changes (read_entry, mark_entry), entries are starred, subscriptions change, or the scheduler prunes old entries. Works over stdio and the HTTP stream. Unchanged fetches only notify `rss://subscriptions` (fetch metadata changed). Delivery is best-effort: a broken listener is skipped, never failed back into the mutation.

## Durable scheduling (Temporal)

The built-in scheduler is in-process: its timer dies with the container. For durable periodic refresh, `scheduler/` is a separate Temporal worker that drives the MCP HTTP endpoint — a persistent Schedule fans out into one child workflow per feed, each with its own retry policy (1m→1h backoff, 5 attempts) and execution history. A failing feed never fails the batch, and everything survives restarts. See [scheduler/README.md](scheduler/README.md) and `deploy/temporal/docker-compose.yml` (Temporal server + UI + worker). When Temporal drives refresh, leave `RSS_MCP_REFRESH_INTERVAL` unset on the server to avoid double fetching.

## Docker

The image runs the HTTP transport, stores the database in a `/data` volume, and runs as a non-root user:

```bash
docker build -t rss-mcp .
docker run -d --name rss-mcp --restart unless-stopped -p 127.0.0.1:8000:8000 \
  -e RSS_MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -v rss-mcp-data:/data rss-mcp
```

The container binds `0.0.0.0` internally (required for port mapping), so set `RSS_MCP_AUTH_TOKEN` whenever the published port is reachable beyond your machine. Publishing to `127.0.0.1` as above keeps it local. Run exactly one container per database volume.

## Environment

| Variable | Default |
| --- | --- |
| `RSS_MCP_DB` | `~/.local/share/rss-mcp/rss.db` (SQLite path) |
| `RSS_MCP_DB_URL` | unset; overrides `RSS_MCP_DB` — `sqlite:///path` or `postgresql://user:pass@host/db` |
| `RSS_MCP_USER_AGENT` | `rss-mcp/<version> (+https://github.com/huangcheng/rss-mcp)` (derived from the package version) |
| `RSS_MCP_AUTH_TOKEN` | unset (HTTP bearer auth disabled) |
| `RSS_MCP_AUTH_TOKENS` | unset (comma-separated extra tokens) |
| `RSS_MCP_FETCH_CONCURRENCY` | `8` |
| `RSS_MCP_REFRESH_INTERVAL` | unset (no background refresh; seconds when set) |
| `RSS_MCP_DISABLE_AFTER_FAILURES` | `5` (`0` never auto-disable) |
| `RSS_MCP_RETENTION_DAYS` | unset (keep entries forever) |
| `RSS_MCP_LOG_FORMAT` | unset (human-readable logs); `json` writes one JSON object per line to stderr, with `trace_id`/`span_id` when OTel is on and `mcp_method`/`mcp_name` for HTTP requests |
| `RSS_MCP_METRICS_TOKEN` | unset (`/metrics` disabled; set it to enable a bearer-protected scrape endpoint) |
| `RSS_MCP_RATE_LIMIT_PER_MINUTE` | `120` (`0` disables; HTTP only) |
| `RSS_MCP_OTEL_OTLP_ENDPOINT` | unset (traces off); set to an OTLP/HTTP trace URL (e.g. `http://tempo:43210/v1/traces`) to enable — requires the `[otel]` extra |

`list_entries` and `search_entries` use `limit` 20 when omitted and clamp to 0–100. Each fetch upserts at most 50 entries and sends stored `ETag` / `Last-Modified` when the feed has them.

## Database engines

SQLite is the default: one file, WAL mode, FTS5 full-text search, zero setup. PostgreSQL is supported for shared/central deployments:

```bash
uv tool install 'rss-mcp-server[postgres]'   # or: pip install 'rss-mcp-server[postgres]'
RSS_MCP_DB_URL='postgresql://user:pass@host:5432/rss' rss-mcp --transport streamable-http
```

Both engines implement the same behavior: subscriptions, entries, read/star state, tag filtering, ranked full-text search with match snippets (FTS5 `bm25`/`snippet` on SQLite, `tsvector`/`ts_rank`/`ts_headline` on PostgreSQL), retention pruning, and cascade deletes. Diacritics are folded on both engines (SQLite's `unicode61` tokenizer; PostgreSQL's `unaccent` extension when the database role may create it — restricted deployments fall back to accent-sensitive `simple` matching). Schema creation and migrations run automatically at startup on either engine. The `postgres` extra is optional — the core package keeps zero database dependencies beyond the standard library.

## Tools

- `add_subscription` — Add a feed by URL with optional tags, per-feed `user_agent`/`headers`, keyword filters, `muted`, and `fetch_interval` (minimum seconds between refreshes). Repeating a URL returns the existing subscription.
- `update_subscription` — Edit title/tags/user_agent/headers/disabled/muted/filters/fetch_interval. Omitted fields are unchanged; empty string/dict/list clears; `disabled=false` re-enables and resets failures; keyword rule changes re-apply to cached entries; `fetch_interval` (seconds) sets a per-feed minimum between refreshes, `0` clears it.
- `remove_subscription` — Delete a subscription and its cached entries.
- `list_subscriptions` — List subscriptions with unread counts and fetch health, optionally filtered by `tag`. `failing_only=true` returns errored/auto-disabled feeds, each with a `remediation` hint (retry, set a browser `user_agent`, or remove + re-add).
- `server_status` — Self-inspection in one call: database counts (entries/unread/starred/filtered/hidden duplicates), feed-health buckets (including feeds that fetch fine but store nothing), scheduling and retention configuration, the last prune run, a `reading` section (`pending`, `delivered_24h`, per-tag pending), feeds needing attention, and a `warnings` list (stalled fetching, retention that never ran, failing feeds, empty feeds). Empty warnings means nothing looks wrong. The same payload backs `rss-mcp status` and `/metrics`.
- `fetch_feed` — Fetch one subscription, or every subscription concurrently when the id is omitted (disabled, backing-off, inside-`fetch_interval`/RSS-`ttl`, or in RSS `skipHours`/`skipDays` windows are skipped in bulk; fetching by id re-enables and bypasses the gates).
- `list_entries` — List cached entries (article bodies omitted). Cross-feed duplicates and keyword-filtered entries are hidden; muted subscriptions drop out of aggregate views (explicit `subscription_id` keeps them). Filter by `tag` or `starred`; `since` accepts ISO 8601. Limit falls back to the server default and is clamped.
- `read_entry` — Return one entry with full content and enclosures. `fetch_full` replaces content only when article text is returned. `mark_read` marks it read.
- `search_entries` — Full-text search over cached title, summary, and content, bm25-ranked with a plain-text match `snippet` (bodies omitted; use `read_entry` for full text). Filter by `tag` or `since` (ISO 8601). Duplicates, filtered entries, and muted feeds are excluded exactly like `list_entries`.
- `get_digest` — One-call unread briefing; repeated calls show only entries no earlier digest handed out (`include_delivered=true` returns everything, `replay_days=N` re-offers items last delivered more than N days ago, `mark_read=true` marks exactly what was returned, sparing starred entries): per-subscription unread groups plus the most recent unread entries (deduped, mute/filter-aware, bodies omitted), scoped by `tag`/`since`/`max_entries`. `per_feed` (default 3) caps each source's contribution so high-volume feeds cannot monopolize the list. Pair with `read_entry` and `mark_entry` catch-up.
- `mark_entry` — Mark entries read/unread by `entry_ids`, a whole `subscription_id`, a `tag`, entries published `before` an ISO 8601 timestamp, or the whole library with `all=true` (catch-up). Sweeps skip starred entries; use `entry_ids` to change those. Unknown ids are skipped.
- `star_entry` — Star (pin) entries. Starred entries are spared by catch-up sweeps and retention pruning.
- `import_opml` — Add each OPML feed, preserving folder names as tags. Existing URLs are skipped.
- `export_opml` — Export subscriptions as an OPML document, grouped by first tag.
- `discover_feed` — Find RSS/Atom feed URLs for a site: advertised link tags first, then common feed paths. Pass a candidate url to `add_subscription`.

## Resources

- `rss://subscriptions` — JSON: all subscriptions with unread counts and fetch health.
- `rss://entries/unread` — JSON: most recent unread entries across subscriptions (bodies omitted, cross-feed duplicates and filtered entries excluded).

**Cross-feed dedup**: the same article syndicated by several feeds is stored once per feed but shown once overall — entries are matched by normalized URL (scheme unified to https, tracking params dropped) or normalized title within a 30-day window, and the earliest copy wins. Unread counts exclude hidden copies. Deleting the winning copy re-elects the next one. A copy readers cannot see (keyword-filtered, or in a muted feed) never holds the winning seat: it is released and the next visible copy is promoted.

## Prompts

- `daily-digest` — Brief the user on unread entries: instructs the host model to call `get_digest`, summarize per subscription, offer `read_entry` for detail, and suggest `mark_entry` catch-up. Takes `max_entries` and optional `tag`.

## CI and releases

GitHub Actions runs the test suite on Python 3.11–3.13 (`.github/workflows/ci.yml`). Pushing a `v*` tag builds, secret-scans, and publishes to PyPI via **Trusted Publishing** (`.github/workflows/release.yml`) — no API tokens. One-time setup: on PyPI, add a pending trusted publisher for the project (owner `huangcheng`, repository `rss-mcp`, workflow `release.yml`, environment `pypi`).

## Live tests

Optional network tests against a real Inoreader OPML export:

```bash
uv run pytest tests/test_real_inoreader_opml.py -v -s
```

They skip when `/Users/huangcheng/Downloads/Cheng-Inoreader-feeds-final-20261016.opml` is missing. Unit tests do not require that file.

## License

[MIT](LICENSE) © 2026 HUANG Cheng
