# Roadmap

Principles: agent-only (no GUI/TUI), one SQLite file by default, lightweight core dependencies.

## 0.2.0 — released 2026-11-19

- **Multi-engine storage**: SQLite (default) or PostgreSQL via `RSS_MCP_DB_URL`, dialect layer (FTS5 ↔ tsvector, json_each ↔ jsonb, null-safe inequality), optional `rss-mcp-server[postgres]` extra, automatic schema migration on both engines.
- **`discover_feed` tool**: finds feed URLs from advertised `<link rel=alternate>` tags (honoring `<base href>`) and common-path probes; bounded reads, off-loop parsing, concurrent probes.
- **Cross-feed dedup**: canonical URL/title keys, 30-day window, earliest copy wins, keeper re-election on delete/prune, legacy backfill at startup; `include_duplicates` escape hatch on `list_entries`/`search_entries`.

## 0.3.0 — released 2026-11-19

- **Per-feed filter rules and mute**: keyword include/exclude lists (case-insensitive substring over title+summary) hide entries store-wide but retroactively and reversibly — rules re-apply to cached entries on edit. `muted` keeps a feed fetched while hiding it from aggregate views and unread counts.
- **`get_digest` tool**: one-call unread briefing (per-subscription groups + newest unread entries, dedup/mute/filter-aware) so hosts stop reassembling digests client-side.
- **Bulk catch-up and starring**: `mark_entry` sweeps by tag, before-timestamp, or `all=true`; `star_entry` pins entries — sweeps and retention pruning spare starred entries.
- **Live notifications**: `subscriptions/listen` is served; every mutation that changes a resource pushes `notifications/resources/updated` for it (fetches, read-state changes, stars, subscription edits, pruning). Delivery is best-effort and isolated per listener.
- **`daily-digest` prompt**: thin template driving `get_digest`; data lives in the tool so the prompt stays token-cheap.

## 0.3.1 — released 2026-11-19

- **Digest diversity**: `get_digest(per_feed=3)` caps each source's contribution; copies stand in for keepers that sit deeper than the per-feed window, so syndicated articles never vanish from briefings.
- **Plain-text snippets**: search snippets are cleaned with a real HTML parser (lxml), preserving escaped literals and dropping script/style bodies; residual angle brackets from mid-tag windows are neutralized.
- **Filter parity**: `tag`/`starred` on `list_entries`; `tag`/`since` on `search_entries`.
- **Feed triage**: `list_subscriptions(failing_only=true)` with per-feed `remediation` hints (status-anchored classification, TLS checked before HTTP codes).

## 0.3.2 — released 2026-11-19

- **Per-feed fetch intervals**: `fetch_interval` (seconds) on add/update_subscription gates bulk fetches per source (weekly feeds stop being knocked every 30 minutes); manual fetch by id always bypasses; `0` clears back to the global cadence.
- **CI uvx stdio smoke test**: builds the wheel, installs via uvx, runs a full MCP session against a local fixture feed — the host install path is now guarded on every push.
- **CI matrix extended to Python 3.14/3.15**; dependabot weekly for uv (root + scheduler) and GitHub Actions.

## 0.3.3 — released 2026-11-19

- **RSS 2.0 spec alignment** (rssboard.org/rss-specification): guid `isPermaLink` fallback when an item has no link; enclosure URLs restricted to http(s) per spec; channel `ttl` honored as the default refresh interval (explicit `fetch_interval` wins); `skipHours`/`skipDays` GMT hints gate bulk refreshes on both the server and the Temporal scheduler (manual fetch by id still bypasses). Hints are parsed directly from XML because feedparser flattens the skip lists; stored per subscription and preserved across 304s.

## 0.3.4 — released 2026-11-19

- **OpenTelemetry traces**: optional `[otel]` extra + `RSS_MCP_OTEL_OTLP_ENDPOINT` enables OTLP/HTTP trace export. Every inbound MCP request is traced by the SDK's built-in middleware (gen_ai semantic conventions, W3C trace-context extraction); custom `rss.feed_fetch` and `rss.refresh_all` spans carry feed host/status/duration/304 outcome. Without an endpoint or the extra, all spans stay no-ops. Metrics deferred to 0.5.x.

## 0.4.0 — released 2026-11-20

- **`server_status` tool + `rss-mcp status` CLI**: one self-inspection payload — database counts, feed-health buckets, scheduling/retention configuration, the last prune run, feeds needing attention, and `warnings` for the silent-failure cases (retention that never ran or went stale, no successful fetch in a day, a library that has never fetched, failing feeds). Deliberately paused feeds are not warnings. A `server_meta` table records every prune cycle, including no-ops.
- **Prometheus `/metrics`**: state, not traffic (counts, per-host last success and consecutive failures, prune liveness and cumulative deletions, retention window, warning count), bearer-protected by `RSS_MCP_METRICS_TOKEN` and absent when unset; one throwaway connection per scrape.
- **Structured logging**: `RSS_MCP_LOG_FORMAT=json` emits one JSON object per line on stderr with `trace_id`/`span_id` from OTel and `mcp_method`/`mcp_name` per request; uvicorn hands its loggers back to the root handler.
- **`export-db` / `import-db`**: engine-agnostic JSONL export and import (read/star state, duplicate links, `--since`, `--no-content`, merge or `--replace`), which makes `sqlite -> postgres` migration a two-command operation.
- **CLI subcommands**: `serve` stays the default, so flag-only invocations (and the Docker image) are unchanged; the image now keeps its flags in `CMD` so `docker run <image> status` works.

## 0.5.0 — released 2026-11-20

- **Incremental digests**: `digest_deliveries` (both engines, cascade on entry delete) records what `get_digest` handed out; repeat calls return only new entries. `include_delivered`, `replay_days=N`, `mark_read=true` (starred entries spared), and `delivered`/`marked_read`/`pending` in the response. Delivery filtering happens in SQL, before each feed's `LIMIT`.
- **Reading state**: `server_status.reading` (`pending`, `delivered_24h`, `by_tag`), plus metrics `rss_mcp_digest_pending`, `rss_mcp_digest_delivered_24h`, `rss_mcp_tag_pending{tag}`.
- **Empty-feed detection**: feeds that fetch successfully but store no entries are a distinct bucket (`feeds.empty`, `rss_mcp_subscriptions{state="empty"}`) and a warning — the silent case that hid 10 feeds on the live library.

## 0.5.1 — released 2026-11-20

- `subscriptions.last_item_count` is nullable and means "items accepted by the last parsed fetch"; NULL (no fetch recorded yet) is not "zero items". 0.5.0 shipped the column as `NOT NULL DEFAULT 0`, which made every pre-existing feed look empty (79 false positives on the live library) until its next fetch. PostgreSQL startup now relaxes the column and clears the sentinel once.

## 0.5.x — remaining robustness work

- `curl_cffi` (TLS impersonation) stays deferred. 2026-11-20: the live library had 0 failing and 0 empty feeds, and 24h of JSON logs contained no 403/429. Revisit only when a feed you want to keep fails with 403/429 plus a Cloudflare challenge; deleting the feed is still the cheaper answer.

## 1.0.0 — stability commitment (drafted 2026-11-20, soak started)

Drafted and implemented ahead of the release, so the contract is enforced from
now on rather than written after the fact:

- `docs/freeze-manifest.json` lists the 15 tool schemas, 2 resource URIs, 1 prompt,
  CLI subcommands/flags, every `RSS_MCP_*` env var and every metric family.
  `tests/test_freeze_manifest.py` compares it against the running server, the parsed
  CLI, the env vars the source reads and an actual scrape — silent drift fails CI.
- `docs/stability.md` defines the breaking-change set, the two-release deprecation
  window, and the soak gate (14 days without a frozen-surface change, `warnings = 0`,
  two successful backup drills, at least one real digest day).
- `docs/` covers tools, deployment and observability; `docs/README.md` is the index.
- Remaining: MCP protocol revision tracking (currently follows the SDK).

## Not planned

- Multi-user / OAuth / per-user databases (user decision 2026-11-19: single-user product).
- robots.txt checks for feeds (a feed URL is an explicit invitation to syndicate).
- GUI / TUI / interactive CLI.
- Built-in LLM summarization (hosts summarize; the server carries no model dependency).
- Email / webhook delivery (outside the agent-only boundary).