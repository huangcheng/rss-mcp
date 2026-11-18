# Roadmap

Principles: agent-only (no GUI/TUI), one SQLite file by default, lightweight core dependencies.

## 0.2.0 — released 2026-11-19

- **Multi-engine storage**: SQLite (default) or PostgreSQL via `RSS_MCP_DB_URL`, dialect layer (FTS5 ↔ tsvector, json_each ↔ jsonb, null-safe inequality), optional `rss-mcp-server[postgres]` extra, automatic schema migration on both engines.
- **`discover_feed` tool**: finds feed URLs from advertised `<link rel=alternate>` tags (honoring `<base href>`) and common-path probes; bounded reads, off-loop parsing, concurrent probes.
- **Cross-feed dedup**: canonical URL/title keys, 30-day window, earliest copy wins, keeper re-election on delete/prune, legacy backfill at startup; `include_duplicates` escape hatch on `list_entries`/`search_entries`.

## 0.3.x — agent experience

- Per-feed filter rules: keyword include/exclude, mute a subscription without deleting it.
- MCP prompts: a `daily-digest` template hosts can invoke for an unread briefing.
- Live notifications: `subscriptions/listen` plus `notifications/resources/updated` when new entries land, so hosts stop polling.

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