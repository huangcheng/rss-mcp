"""JSON log format: shape, request context, and the opt-in switch."""

from __future__ import annotations

import json
import logging

import pytest

from rss_mcp.logging_setup import JsonFormatter, configure_logging, request_context


@pytest.fixture()
def restore_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_propagate = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, (handlers, propagate) in saved_propagate.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate


def test_json_format_carries_standard_fields():
    record = logging.LogRecord(
        name="rss_mcp", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="fetch failed for %s", args=("host",), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "rss_mcp"
    assert payload["msg"] == "fetch failed for host"
    assert payload["ts"].endswith("Z")


def test_json_format_includes_request_context():
    token = request_context.set({"mcp_method": "tools/call", "mcp_name": "fetch_feed"})
    try:
        payload = json.loads(
            JsonFormatter().format(logging.LogRecord("rss_mcp", logging.INFO, __file__, 1, "hi", (), None))
        )
    finally:
        request_context.reset(token)
    assert payload["mcp_method"] == "tools/call"
    assert payload["mcp_name"] == "fetch_feed"


def test_configure_logging_is_opt_in(restore_logging):
    root = logging.getLogger()
    before = root.handlers[:]
    assert configure_logging(None) is False
    assert configure_logging("text") is False
    assert root.handlers == before


def test_configure_logging_installs_the_json_handler(restore_logging):
    assert configure_logging("JSON") is True
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    # uvicorn's handlers are dropped so its lines reach the root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate is True
        assert logging.getLogger(name).handlers == []


def test_log_lines_are_json_on_stderr(restore_logging, capsys):
    assert configure_logging("json") is True
    logging.getLogger("rss_mcp").info("prune loop removed %d expired entries", 3)
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err.strip())
    assert payload["msg"] == "prune loop removed 3 expired entries"


@pytest.mark.asyncio
async def test_request_middleware_tags_mcp_method():
    from rss_mcp.server import _RequestLogContext

    seen: dict[str, object] = {}

    async def spy(scope, receive, send):
        seen.update(request_context.get())

    await _RequestLogContext(spy)(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"mcp-method", b"tools/call"), (b"mcp-name", b"fetch_feed")],
        },
        None,
        None,
    )
    assert seen == {
        "http_method": "POST",
        "http_path": "/mcp",
        "mcp_method": "tools/call",
        "mcp_name": "fetch_feed",
    }
    # The context is reset when the request finishes.
    assert request_context.get() == {}