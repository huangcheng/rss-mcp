"""OpenTelemetry setup: OTLP trace export when an endpoint is configured.

The MCP SDK already wraps every inbound request in a span via its built-in
OpenTelemetryMiddleware (opentelemetry-api is a hard SDK dependency). This
module only installs a TracerProvider with an OTLP exporter; without an
endpoint — or without the [otel] extra installed — all spans stay no-ops.
"""

from __future__ import annotations

import logging

from rss_mcp import __version__
from rss_mcp.config import Settings

logger = logging.getLogger("rss_mcp")

_provider = None


def setup_telemetry(settings: Settings) -> bool:
    """Install the OTLP TracerProvider; returns True when trace export is active."""
    global _provider
    endpoint = settings.otel_otlp_endpoint
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "RSS_MCP_OTEL_OTLP_ENDPOINT is set but the otel extra is not installed; "
            "spans stay no-ops (pip install rss-mcp-server[otel])"
        )
        return False
    resource = Resource.create(
        {"service.name": "rss-mcp", "service.version": __version__}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("OpenTelemetry trace export enabled -> %s", endpoint)
    return True


def shutdown_telemetry() -> None:
    """Flush queued spans; without this the batch's tail is lost on exit."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None