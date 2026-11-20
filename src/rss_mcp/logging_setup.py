"""Opt-in JSON log lines.

Logs are the only observability surface this server promises to keep, so they
have to be machine-readable when someone asks for it: `RSS_MCP_LOG_FORMAT=json`
switches every logger (uvicorn included) to one JSON object per line on stderr,
with the trace/span ids of the enclosing OpenTelemetry span and the MCP method
of the request that produced the line.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Per-request fields, set by the ASGI middleware; anyio copies the context
# into the tasks a request spawns, so request-scoped logs stay tagged.
request_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "rss_mcp_request_context", default={}
)


def _trace_fields() -> dict[str, str]:
    try:
        from opentelemetry import trace as otel_trace

        span_context = otel_trace.get_current_span().get_span_context()
    except Exception:
        return {}
    if not span_context or not span_context.is_valid:
        return {}
    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(request_context.get())
        payload.update(_trace_fields())
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(value: str | None) -> bool:
    """Install the JSON handler when requested; return whether it was.

    Callers pass the return value to uvicorn as `log_config=None`, because
    uvicorn would otherwise overwrite the root handler it finds here.
    """
    if (value or "").strip().lower() != "json":
        return False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # uvicorn's own loggers carry handlers and `propagate = False` from its
    # dictConfig; hand them back to the root handler so access logs are JSON
    # too instead of plain text spliced into the stream.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    return True
