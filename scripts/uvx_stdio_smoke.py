"""uvx stdio smoke test against a built wheel.

Exercises the exact host path: uvx installs the wheel, spawns the server,
JSON-RPC over stdin/stdout. Uses a local fixture feed so CI never depends
on external sites. Run: uv run python scripts/uvx_stdio_smoke.py dist/*.whl
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>Smoke Feed</title>
<item><guid>smoke-1</guid><title>Smoke article about zephyr</title>
<link>https://smoke.example/1</link><description>zephyr winds</description></item>
</channel></rss>"""


async def main(wheel: str) -> None:
    serve_dir = Path(tempfile.mkdtemp())
    (serve_dir / "feed.xml").write_text(RSS)
    handler = partial(SimpleHTTPRequestHandler, directory=str(serve_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    db = str(Path(tempfile.mkdtemp()) / "smoke.db")
    params = StdioServerParameters(
        command="uvx",
        args=["--from", wheel, "rss-mcp-server"],
        env={**os.environ, "RSS_MCP_DB": db},
    )
    async with Client(params) as client:
        info = client.server_info
        assert info.name == "rss-mcp", info
        tools = await client.list_tools()
        assert len(tools.tools) == 14, [t.name for t in tools.tools]
        resources = await client.list_resources()
        assert len(resources.resources) == 2
        prompts = await client.list_prompts()
        assert len(prompts.prompts) == 1

        sub = await client.call_tool(
            "add_subscription", {"url": f"http://127.0.0.1:{port}/feed.xml"}
        )
        sid = sub.structured_content["id"]
        fetched = await client.call_tool("fetch_feed", {"subscription_id": sid})
        assert fetched.structured_content["results"][0]["status"] == "ok"

        entries = await client.call_tool("list_entries", {"subscription_id": sid})
        eid = entries.structured_content["entries"][0]["id"]
        found = await client.call_tool("search_entries", {"query": "zephyr"})
        assert found.structured_content["entries"]
        read = await client.call_tool("read_entry", {"entry_id": eid})
        assert read.structured_content["title"]

        await client.call_tool("mark_entry", {"entry_ids": [eid], "read": True})
        await client.call_tool("star_entry", {"entry_ids": [eid], "starred": True})
        digest = await client.call_tool("get_digest", {"max_entries": 5})
        assert digest.structured_content["total_unread"] == 0
        opml = await client.call_tool("export_opml", {})
        assert "feed.xml" in opml.structured_content["opml"]
        removed = await client.call_tool("remove_subscription", {"subscription_id": sid})
        assert removed.structured_content["ok"] is True

    httpd.shutdown()
    print(f"uvx stdio smoke: ALL PASS ({info.name} {info.version})")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))