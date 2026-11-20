"""Rewrite the MCP section of docs/freeze-manifest.json from the live server.

Only `mcp.tools` / `mcp.resources` / `mcp.prompts` are regenerated; `env`,
`metrics` and `cli` stay manual because they are contracts this script cannot
discover. Run it after a deliberate signature change, then review the diff:
the manifest is the 1.0 promise, not a build artifact.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from mcp import Client

from rss_mcp.config import Settings
from rss_mcp.server import create_server

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "freeze-manifest.json"


async def collect() -> dict:
    settings = Settings(
        db_path=Path(tempfile.mkdtemp()) / "rss.db", user_agent="rss-mcp/manifest"
    )
    async with Client(create_server(settings)) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        prompts = (await client.list_prompts()).prompts
    return {
        "tools": [
            {"name": tool.name, "inputSchema": tool.input_schema} for tool in tools
        ],
        "resources": [str(resource.uri) for resource in resources],
        "prompts": [prompt.name for prompt in prompts],
    }


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    manifest["mcp"] = asyncio.run(collect())
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"updated {MANIFEST.relative_to(ROOT)}")
    print("review the diff: every frozen change must be a deliberate decision")


if __name__ == "__main__":
    main()
