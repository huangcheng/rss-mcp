"""The 1.0 freeze manifest is a contract: drift must fail CI.

`docs/freeze-manifest.json` lists the surfaces a 1.0 host may depend on. This
test compares it against the running server, the parsed CLI, the environment
variables the source reads, and the metrics a scrape actually emits. A change
that is not reflected in the manifest (and therefore not a deliberate,
reviewable act) fails here.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest
from mcp import Client

from rss_mcp import server as server_module
from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.metrics import render_metrics
from rss_mcp.models import EntryInput
from rss_mcp.store import Store

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "freeze-manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.mark.asyncio
async def test_manifest_matches_the_served_mcp_surface(manifest):
    settings = Settings(
        db_path=Path(tempfile.mkdtemp()) / "rss.db", user_agent="rss-mcp/freeze-test"
    )
    async with Client(server_module.create_server(settings)) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        prompts = (await client.list_prompts()).prompts

    served = {tool.name: tool.input_schema for tool in tools}
    frozen = {tool["name"]: tool["inputSchema"] for tool in manifest["mcp"]["tools"]}
    assert set(served) == set(frozen)
    for name, schema in frozen.items():
        assert served[name] == schema, f"{name} input schema changed; update the manifest"
    assert sorted(str(resource.uri) for resource in resources) == sorted(
        manifest["mcp"]["resources"]
    )
    assert sorted(prompt.name for prompt in prompts) == sorted(manifest["mcp"]["prompts"])


def test_manifest_matches_the_cli(manifest):
    parser = server_module.build_parser()
    for command in manifest["cli"]["subcommands"]:
        args = parser.parse_args([command])
        assert args.command == command

    parsed = parser.parse_args(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.2",
            "--port",
            "9001",
            "--path",
            "/x",
        ]
    )
    assert (parsed.host, parsed.port, parsed.path) == ("127.0.0.2", 9001, "/x")
    # Flag-only invocation still means `serve`, the documented default.
    assert parser.parse_args(["--port", "9002"]).command == manifest["cli"]["default_command"]

    for flag in manifest["cli"]["serve_flags"]:
        assert any(flag in action.option_strings for action in parser._actions), flag


def test_manifest_covers_every_rss_mcp_environment_variable(manifest):
    pattern = re.compile(r"RSS_MCP_[A-Z0-9_]+")
    referenced: set[str] = set()
    for source in ("src/rss_mcp/config.py", "src/rss_mcp/server.py"):
        referenced |= set(pattern.findall((ROOT / source).read_text()))
    assert referenced == set(manifest["env"])


def test_manifest_covers_every_emitted_metric(manifest, tmp_path):
    settings = Settings(db_path=tmp_path / "rss.db", user_agent="rss-mcp/freeze-test")
    conn = connect(settings.db_path)
    init_db(conn)
    store = Store(conn)
    sub = store.add_subscription("https://freeze.example/feed", tags=["freeze"])
    store.upsert_entries(
        sub.id,
        [
            EntryInput(guid="one", title="One", published_at="2026-11-01T00:00:00Z"),
            # One undelivered entry keeps the per-tag family in the scrape.
            EntryInput(guid="two", title="Two", published_at="2026-11-02T00:00:00Z"),
        ],
    )
    store.update_fetch_meta(
        sub.id,
        etag=None,
        last_modified=None,
        last_fetched_at="2026-11-20T00:00:00Z",
        last_error=None,
        item_count=1,
    )
    store.record_prune(1)
    entry = store.list_entries(subscription_id=sub.id, limit=10)[-1]
    store.record_deliveries([entry.id])
    body = render_metrics(settings, store)
    conn.close()

    emitted = set(re.findall(r"^# TYPE (\S+) ", body, flags=re.MULTILINE))
    assert emitted, "no metric families rendered"
    # PostgreSQL-only families cannot appear in a SQLite scrape; they are
    # checked by test_postgres.py::test_lock_health_metrics_on_postgres.
    pg_only = {
        "rss_mcp_db_idle_in_transaction",
        "rss_mcp_db_lock_waiters",
        "rss_mcp_db_connections",
    }
    # The seeded library exercises every conditional family, so the scrape is a
    # complete inventory; a new or renamed family must be frozen deliberately.
    assert emitted == set(manifest["metrics"]) - pg_only
    assert pg_only <= set(manifest["metrics"])