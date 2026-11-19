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

## 0.4.x — multi-user and auth

- Per-user databases (or row-level ownership) with token → user mapping.
- OAuth2 via the SDK's native auth server; token scopes (read-only hosts vs management hosts).
- Per-token rate-limit tiers.

## 0.5.x — robustness and observability

- Prometheus `/metrics` (fetch latency, failure rates, queue depth).
- Structured logging; `export-db` / `import-db` backup and migration tools.
- Optional `curl_cffi` extra for TLS-impersonation fetches against bot-walled feeds.
- Python 3.14/3.15 support; dependabot for dependency updates.

## 1.0.0 — stability commitment

- Frozen tool signatures and response shapes; breaking changes go through a deprecation cycle.
- Full documentation site.
- Tracking mechanism for new MCP protocol revisions.

## Not planned

- robots.txt checks for feeds (a feed URL is an explicit invitation to syndicate).
- GUI / TUI / interactive CLI.
- Built-in LLM summarization (hosts summarize; the server carries no model dependency).
- Email / webhook delivery (outside the agent-only boundary).