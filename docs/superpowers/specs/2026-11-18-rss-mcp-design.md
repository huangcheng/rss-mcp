# RSS MCP — Design

**Date:** 2026-11-18  
**Status:** Approved (2026-11-18) — product name `rss-mcp`

## Intent

Build a Python MCP server that lets an AI agent manage RSS/Atom subscriptions, refresh feeds, list/read/search entries, and track read status. Run over **stdio**, packaged for **`uvx`**.

## Why

Agents need a durable, queryable feed store—not one-off URL fetches. Subscriptions, unread state, and full-text search over cached content require a local database.

## Goals

- MCP tools: subscribe management, fetch, list, read, search, mark read/unread
- Persistent subscriptions + entry cache + read state
- Fast keyword search over cached entry text
- Conditional HTTP fetch (`ETag` / `Last-Modified`) where feeds support it
- OPML import/export
- Per-feed / global limits so tool results stay agent-sized
- Install/run via `uvx` with stdio transport
- **Strict conformance** to MCP specification revision **[2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)**

## Non-goals

- Multi-user / cloud sync
- Any human-facing UI: GUI, web, or TUI/CLI interactive shells — this package is **agent-only** (MCP tools over stdio)
- MCP **Resources**, **Prompts**, or **MCP Apps** UI surfaces (v1 is tools-only)
- Adopting deprecated protocol features as primary design (Roots, Sampling, Logging, legacy HTTP+SSE)
- Push notifications
- Built-in LLM summarization (the host model does that)
- Guaranteed full-article extraction for every site (readability is best-effort, opt-in)

## Anti-goals

- Shipping prompts, menus, or interactive terminal UX that compete with the MCP tool surface
- Hand-rolling JSON-RPC / inventing non-spec methods or message shapes
- Polluting stdio MCP framing (e.g. `print` to stdout)
- Rewriting the whole store on every mark-read (rules out JSON-as-primary-store)
- Returning unbounded feed dumps that blow the context window

## Constraints

- Language: Python 3.11+ (required by official MCP Python SDK v2)
- **Protocol:** MUST implement [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) correctly; authoritative schema is the spec’s TypeScript schema
- **SDK:** Official **MCP Python SDK v2** (`mcp` ≥ 2.x) — do not use v1-only APIs as the primary path
- Transport: **stdio only** (v1), per [stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- Packaging: `uv` project, runnable as `uvx …`
- Storage: single local SQLite file (path overridable via env) — **application** state; protocol remains request/response (stateless wire)
- No separate DB server

## MCP compliance (2026-07-28)

Hard requirements for this project:

1. **Wire format (stdio):** newline-delimited JSON-RPC 2.0; messages MUST NOT contain embedded newlines; **stdout is MCP messages only**; logging goes to **stderr**; exit promptly when stdin closes.
2. **Stateless protocol core:** no dependence on a session id or a mandatory `initialize` handshake for 2026-07-28 clients; support `server/discover` and self-describing request `_meta` as defined by the spec/SDK.
3. **Tools feature only (v1):** declare the `tools` capability; implement `tools/list` and `tools/call` per [server tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).
4. **Tool definitions:** each tool has `name`, `description`, valid JSON Schema `inputSchema` (object; empty-args tools use an explicit empty object schema); optional `outputSchema` / annotations when useful; deterministic `tools/list` order; honor list cache fields (`ttlMs` / `cacheScope`) via the SDK.
5. **Tool calls:** validate inputs; return spec-shaped results (including `resultType`, `content` blocks, `isError` as applicable). Tool failures that are business errors SHOULD use structured tool results with `isError: true` where appropriate; protocol/JSON-RPC errors for malformed requests.
6. **Cancellation:** honor `notifications/cancelled` for in-flight work (e.g. long `fetch_feed` / readability) when using stdio.
7. **SDK boundary:** prefer SDK types and server APIs over custom framing so behavior tracks the Tier-1 Python SDK’s 2026-07-28 implementation.
8. **Extensions:** do not require Tasks, Skills, or MCP Apps for v1; may add later only as explicit opt-in capability negotiation.

Note: “stateless protocol” does **not** forbid SQLite. Durable subscription/entry state is application data; clients pass ids/filters as tool arguments (no hidden transport session).

## Approaches considered

| Approach | Tradeoff | Decision |
|----------|----------|----------|
| JSON file store | Simple inspect/edit; poor for FTS, unread updates, concurrent refresh | Rejected |
| SQLite (+ FTS5) | Single file, strong query/FTS, fits unread + search | **Chosen** |
| External DB (Postgres, etc.) | Overkill for personal/local MCP; hurts `uvx` story | Rejected |
| Network-only, no cache | No durable unread/search | Rejected |

## Architecture

```
Host (Cursor / Claude / etc.)
    │ stdio — MCP 2026-07-28 (JSON-RPC)
    ▼
rss-mcp (Python + official mcp SDK v2)
    ├── tools/list + tools/call (only MCP feature in v1)
    ├── feed client (HTTP + feedparser)
    ├── readability (optional page fetch)
    └── SQLite (subs, entries, state, FTS5)  ← app state, not protocol session
```

### Database (builder owns exact schema)

Suggested tables:

- **subscriptions** — id, url, title, tags/folders, etag, last_modified, last_fetched_at, error, created_at
- **entries** — id, subscription_id, guid, url, title, author, published_at, summary, content, fetched_at
- **entry_state** — entry_id, read_at (null = unread), starred (optional)
- **entries_fts** — FTS5 over title, summary, content (external-content or content-sync triggers)

Unique key on `(subscription_id, guid)` or normalized URL when guid missing.

DB default path: XDG-style e.g. `~/.local/share/rss-mcp/rss.db`, override with `RSS_MCP_DB`.

### MCP tools (v1)

| Tool | Purpose |
|------|---------|
| `add_subscription` | Add feed by URL; optional title/tags |
| `remove_subscription` | Delete subscription and its entries/state |
| `list_subscriptions` | List feeds (+ unread counts) |
| `fetch_feed` | Refresh one or all; respect ETag/Last-Modified; upsert entries |
| `list_entries` | Filter by feed, unread-only, limit, since |
| `read_entry` | Return full cached content; optional live readability fetch |
| `search_entries` | FTS over cached text; filters for feed/unread/limit |
| `mark_entry` | Mark read / unread (batch by ids ok) |
| `import_opml` | Import subscriptions from OPML |
| `export_opml` | Export current subscriptions |

### Fetch behavior

- User-Agent identifiable as rss-mcp
- Timeout and max response size caps
- Store HTTP validators; on 304, update `last_fetched_at` only
- Cap new items per fetch (configurable, sensible default)

### Read / content

- Prefer feed-provided `content:encoded` / Atom content, else summary
- `read_entry(fetch_full=true)` may fetch the article URL and extract main text (best-effort); cache result when successful
- Never block the whole server on a single slow page beyond timeout

### Search

- SQLite **FTS5** over title + summary + content
- Default rank by relevance; optional recency blend left to builder
- Always accept `limit` (default small, e.g. 20)

### Packaging / UX

- Project name: `rss-mcp` (confirm at publish time)
- Entry point console script that runs the MCP stdio server
- Document Cursor/`mcp.json` snippet using `uvx`
- Env: `RSS_MCP_DB`, optional `RSS_MCP_USER_AGENT`

## What’s left to the builder

- Exact table DDL, migrations (or create-on-start), FTS trigger strategy
- Feed library choice (e.g. `feedparser`) and HTTP client
- Readability library / fallback strategy
- Tool argument schemas; map domain errors onto MCP tool result / JSON-RPC error correctly
- Pin compatible `mcp` (v2) version; follow current SDK server entrypoint (`MCPServer` / `run(transport="stdio")` or equivalent in the installed docs)
- Tests: protocol smoke (discover + tools/list + tools/call over in-memory or stdio) plus fixture feeds
- README with install and MCP host `uvx` config; link the 2026-07-28 spec

## Out of scope for builder without a new decision

- HTTP / Streamable HTTP transport (spec-compliant later; not v1)
- Resources, Prompts, or MCP Apps
- Remote multi-tenant hosting
- Changing primary store away from SQLite
- Targeting a protocol revision other than 2026-07-28 as the primary contract
