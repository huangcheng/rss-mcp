# rss-mcp

Agent-only MCP server for RSS and Atom. A host drives it over stdio (default) or Streamable HTTP. There is no GUI, TUI, or interactive CLI. Subscriptions, cached entries, unread state, and full-text search live in one local SQLite file. Eleven tools plus two read-only resources; no prompts or MCP Apps.

Protocol: [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) (stdio or Streamable HTTP; tools + resources) via the official Python SDK v2 (`mcp` ≥ 2.3).

Requires Python 3.11+. Install from [PyPI](https://pypi.org/project/rss-mcp-server/): `uv tool install rss-mcp-server` gives you the `rss-mcp` and `rss-mcp-server` commands, or run it once with `uvx rss-mcp-server`.

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

## Server behavior

- **Concurrent fetching**: `fetch_feed` without an id refreshes all subscriptions concurrently (`RSS_MCP_FETCH_CONCURRENCY`, default 8).
- **Failure handling**: failed feeds record `last_error` and a failure count, then back off exponentially (60s doubling to 1h) in bulk fetches. After `RSS_MCP_DISABLE_AFTER_FAILURES` consecutive failures (default 5, `0` never) a feed is auto-disabled and skipped until fetched by id or re-enabled via `update_subscription`.
- **Scheduled refresh**: set `RSS_MCP_REFRESH_INTERVAL` (seconds) to refresh all feeds in the background on that interval; unset means fetch-on-demand only.
- **Retention**: set `RSS_MCP_RETENTION_DAYS` to prune entries older than that many days on each scheduled refresh (starred entries are kept). Unset keeps everything.
- **Per-feed overrides**: `add_subscription` / `update_subscription` accept `user_agent` and `headers` for feeds that block the default client.

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
| `RSS_MCP_USER_AGENT` | `rss-mcp/0.1 (+https://github.com/huangcheng/rss-mcp)` |
| `RSS_MCP_AUTH_TOKEN` | unset (HTTP bearer auth disabled) |
| `RSS_MCP_AUTH_TOKENS` | unset (comma-separated extra tokens) |
| `RSS_MCP_FETCH_CONCURRENCY` | `8` |
| `RSS_MCP_REFRESH_INTERVAL` | unset (no background refresh; seconds when set) |
| `RSS_MCP_DISABLE_AFTER_FAILURES` | `5` (`0` never auto-disable) |
| `RSS_MCP_RETENTION_DAYS` | unset (keep entries forever) |
| `RSS_MCP_RATE_LIMIT_PER_MINUTE` | `120` (`0` disables; HTTP only) |

`list_entries` and `search_entries` use `limit` 20 when omitted and clamp to 0–100. Each fetch upserts at most 50 entries and sends stored `ETag` / `Last-Modified` when the feed has them.

## Database engines

SQLite is the default: one file, WAL mode, FTS5 full-text search, zero setup. PostgreSQL is supported for shared/central deployments:

```bash
uv tool install 'rss-mcp-server[postgres]'   # or: pip install 'rss-mcp-server[postgres]'
RSS_MCP_DB_URL='postgresql://user:pass@host:5432/rss' rss-mcp --transport streamable-http
```

Both engines implement the same behavior: subscriptions, entries, read/star state, tag filtering, ranked full-text search with match snippets (FTS5 `bm25`/`snippet` on SQLite, `tsvector`/`ts_rank`/`ts_headline` on PostgreSQL), retention pruning, and cascade deletes. Diacritics are folded on both engines (SQLite's `unicode61` tokenizer; PostgreSQL's `unaccent` extension when the database role may create it — restricted deployments fall back to accent-sensitive `simple` matching). Schema creation and migrations run automatically at startup on either engine. The `postgres` extra is optional — the core package keeps zero database dependencies beyond the standard library.

## Tools

- `add_subscription` — Add a feed by URL with optional tags, per-feed `user_agent`/`headers`. Repeating a URL returns the existing subscription.
- `update_subscription` — Edit title/tags/user_agent/headers/disabled. Omitted fields are unchanged; empty string/dict/list clears; `disabled=false` re-enables and resets failures.
- `remove_subscription` — Delete a subscription and its cached entries.
- `list_subscriptions` — List subscriptions with unread counts and fetch health, optionally filtered by `tag`.
- `fetch_feed` — Fetch one subscription, or every subscription concurrently when the id is omitted (disabled/backing-off feeds are skipped in bulk; fetching by id re-enables).
- `list_entries` — List cached entries (article bodies omitted). Limit falls back to the server default and is clamped.
- `read_entry` — Return one entry with full content and enclosures. `fetch_full` replaces content only when article text is returned. `mark_read` marks it read.
- `search_entries` — Full-text search over cached title, summary, and content, bm25-ranked with a match `snippet` (bodies omitted; use `read_entry` for full text).
- `mark_entry` — Mark entries read/unread by `entry_ids`, or a whole subscription via `subscription_id`. Unknown ids are skipped.
- `import_opml` — Add each OPML feed, preserving folder names as tags. Existing URLs are skipped.
- `export_opml` — Export subscriptions as an OPML document, grouped by first tag.

## Resources

- `rss://subscriptions` — JSON: all subscriptions with unread counts and fetch health.
- `rss://entries/unread` — JSON: most recent unread entries across subscriptions (bodies omitted).

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
