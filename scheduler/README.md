# rss-mcp-scheduler

Temporal worker that durably refreshes [rss-mcp](https://pypi.org/project/rss-mcp-server/) subscriptions by calling its MCP HTTP endpoint. Separate from the server package: the server stays a simple MCP server; this component owns scheduling, retries, and per-feed execution history.

## How it works

- A Temporal **Schedule** starts `RefreshAllWorkflow` every `RSS_MCP_REFRESH_INTERVAL` minutes (default 30). The schedule lives in Temporal's database — it survives worker and server restarts.
- `RefreshAllWorkflow` lists subscriptions through the MCP API, then runs one **child workflow per feed**. Each child runs a `fetch_feed` activity with a retry policy (1m → 2m → 4m … capped at 1h, up to 5 attempts). A failing feed never fails the batch.
- Activities are thin httpx calls using the 2026-07-28 per-request envelope plus `Authorization: Bearer`. `fetch_feed` is idempotent (ETag + guid upsert), so at-least-once retries are safe.
- Feeds the server has auto-disabled are skipped; app-side `failure_count` still tracks health for `list_subscriptions`.

## Configuration (environment)

| Variable | Default |
| --- | --- |
| `TEMPORAL_ADDRESS` | `localhost:7233` |
| `TEMPORAL_NAMESPACE` | `default` |
| `TEMPORAL_TASK_QUEUE` | `rss-mcp-refresh` |
| `RSS_MCP_URL` | `http://127.0.0.1:8000/mcp` |
| `RSS_MCP_TOKEN` | unset (required when the server enforces bearer auth) |
| `RSS_MCP_REFRESH_INTERVAL` | `30` (minutes) |

## Run

```bash
uv run rss-mcp-worker
```

The worker creates the schedule on startup if it does not exist. Deployment (Temporal server + UI + worker via docker compose, Caddy in front of the UI) lives in `../deploy/temporal/`.

## Tests

```bash
uv run pytest
```