# rss-mcp

Agent-only MCP server for RSS and Atom. A host drives it over stdio (default) or Streamable HTTP. There is no GUI, TUI, or interactive CLI. Subscriptions, cached entries, unread state, and full-text search live in one local SQLite file. v1 is tools only: no resources, prompts, or MCP Apps.

Protocol: [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) (stdio or Streamable HTTP, tools) via the official Python SDK v2 (`mcp` ≥ 2.3). Design: [docs/superpowers/specs/2026-11-18-rss-mcp-design.md](docs/superpowers/specs/2026-11-18-rss-mcp-design.md).

Requires Python 3.11+.

## Host config

In stdio mode, the process reads JSON-RPC from stdin and writes MCP messages only to stdout. It exits when stdin closes.

Cursor `mcp.json` (any stdio host can use the same command):

```json
{
  "mcpServers": {
    "rss-mcp": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/rss-mcp", "rss-mcp"],
      "env": {
        "RSS_MCP_DB": "/absolute/path/rss.db"
      }
    }
  }
}
```

`uvx rss-mcp` once the package is on an index. From this checkout: `uv run rss-mcp`.

## HTTP transport

Run the same server over Streamable HTTP instead of stdio:

```bash
uv run rss-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

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

All sessions share one SQLite database (same `RSS_MCP_DB` setting). The endpoint serves both protocol eras: 2026-07-28 stateless per-request POSTs (with the `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers and `_meta` envelope) and the legacy `initialize` handshake (up to 2025-11-25, with sessions) for older hosts. The server has no authentication: keep the default loopback bind, or put it behind a reverse proxy with auth before exposing it beyond localhost.

## Environment

| Variable | Default |
| --- | --- |
| `RSS_MCP_DB` | `~/.local/share/rss-mcp/rss.db` |
| `RSS_MCP_USER_AGENT` | `rss-mcp/0.1 (+https://github.com/huangcheng/rss-mcp)` |

`list_entries` and `search_entries` use `limit` 20 when omitted and clamp to 0–100. Each fetch upserts at most 50 entries and sends stored `ETag` / `Last-Modified` when the feed has them.

## Tools

- `add_subscription` — Add a feed by URL. Repeating a URL returns the existing subscription.
- `remove_subscription` — Delete a subscription and its cached entries.
- `list_subscriptions` — List subscriptions with unread counts.
- `fetch_feed` — Fetch one subscription, or every subscription when the id is omitted.
- `list_entries` — List cached entries (article bodies omitted). Limit falls back to the server default and is clamped.
- `read_entry` — Return one entry with full content. `fetch_full` replaces content only when article text is returned. `mark_read` marks it read.
- `search_entries` — Full-text search over cached title, summary, and content (bodies omitted; use `read_entry` for full text).
- `mark_entry` — Mark entries read or unread. Unknown ids are skipped.
- `import_opml` — Add each OPML feed. Existing URLs are skipped.
- `export_opml` — Export subscriptions as an OPML document.

## Live tests

Optional network tests against a real Inoreader OPML export:

```bash
uv run pytest tests/test_real_inoreader_opml.py -v -s
```

They skip when `/Users/huangcheng/Downloads/Cheng-Inoreader-feeds-final-20261016.opml` is missing. Unit tests do not require that file.

## License

[MIT](LICENSE) © 2026 HUANG Cheng
