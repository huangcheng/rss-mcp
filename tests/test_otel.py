"""OpenTelemetry: setup gating, no-op safety, span emission."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from rss_mcp.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(db_path=Path("/tmp/otel-test.db"), user_agent="rss-mcp/test")
    base.update(overrides)
    return Settings(**base)


def test_setup_telemetry_disabled_without_endpoint():
    from rss_mcp.telemetry import setup_telemetry

    assert setup_telemetry(_settings()) is False


def test_setup_telemetry_active_with_endpoint_and_extra():
    import subprocess
    import sys
    import textwrap

    # set_tracer_provider is a process-global singleton; run the real setup
    # in a subprocess so it cannot leak an OTLP exporter into this process.
    code = textwrap.dedent(
        """
        from pathlib import Path
        from rss_mcp.config import Settings
        from rss_mcp.telemetry import setup_telemetry
        s = Settings(db_path=Path("/tmp/otel-test.db"), user_agent="t",
                     otel_otlp_endpoint="http://127.0.0.1:43210/v1/traces")
        print(setup_telemetry(s))
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"


@pytest.mark.asyncio
async def test_spans_are_noops_without_provider(server_settings, monkeypatch):
    # Default (no endpoint): spans no-op, tools still work.
    from rss_mcp import server
    from rss_mcp.feed_client import FetchResult

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(
            status="ok", entries=[], etag=None, last_modified=None,
            feed_title=None, error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        sub = await client.call_tool("add_subscription", {"url": "https://noop.example/feed"})
        result = await client.call_tool("fetch_feed", {"subscription_id": sub.structured_content["id"]})
    assert result.structured_content["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_feed_fetch_span_emitted_with_attributes(server_settings, monkeypatch, span_exporter):
    from rss_mcp import server
    from rss_mcp.feed_client import FetchResult

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(
            status="ok", entries=[], etag=None, last_modified=None,
            feed_title=None, error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        sub = await client.call_tool("add_subscription", {"url": "https://span.example/feed"})
        result = await client.call_tool("fetch_feed", {"subscription_id": sub.structured_content["id"]})
    assert result.structured_content["results"][0]["status"] == "ok"

    feed_spans = [s for s in span_exporter if s.name == "rss.feed_fetch"]
    assert len(feed_spans) == 1
    attributes = dict(feed_spans[0].attributes)
    assert attributes["rss.feed.host"] == "span.example"
    assert attributes["rss.fetch.status"] == "ok"
    assert attributes["rss.subscription.id"] == sub.structured_content["id"]
    assert attributes["rss.fetch.upserted"] == 0


@pytest.fixture
def span_exporter():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    spans: list = []

    class CapturingExporter:
        def export(self, span_batch):
            spans.extend(span_batch)
            return 0

        def shutdown(self):
            return None

    # set_tracer_provider is once-per-process: attach a processor to the
    # existing global provider when a previous test already installed one.
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        provider = current
    else:
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    processor = SimpleSpanProcessor(CapturingExporter())
    provider.add_span_processor(processor)
    try:
        yield spans
    finally:
        processor.shutdown()


@pytest.mark.asyncio
async def test_feed_fetch_error_span_attributes(server_settings, monkeypatch, span_exporter):
    from rss_mcp import server
    from rss_mcp.feed_client import FetchResult

    async def failing_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(
            status="error", entries=[], etag=None, last_modified=None,
            feed_title=None, error="HTTP 502",
        )

    monkeypatch.setattr(server, "fetch_feed", failing_fetch)
    async with Client(server.create_server(server_settings)) as client:
        sub = await client.call_tool("add_subscription", {"url": "https://fail.example/feed"})
        result = await client.call_tool("fetch_feed", {"subscription_id": sub.structured_content["id"]})
    assert result.structured_content["results"][0]["status"] == "error"

    feed_spans = [s for s in span_exporter if s.name == "rss.feed_fetch"]
    assert len(feed_spans) == 1
    attributes = dict(feed_spans[0].attributes)
    assert attributes["rss.fetch.status"] == "error"
    assert attributes["rss.fetch.error"] == "HTTP 502"


@pytest.mark.asyncio
async def test_refresh_all_span_with_round_attributes(server_settings, monkeypatch, span_exporter):
    from rss_mcp import server
    from rss_mcp.feed_client import FetchResult

    async def fake_fetch(url: str, **kwargs) -> FetchResult:
        return FetchResult(
            status="ok", entries=[], etag=None, last_modified=None,
            feed_title=None, error=None,
        )

    monkeypatch.setattr(server, "fetch_feed", fake_fetch)
    async with Client(server.create_server(server_settings)) as client:
        await client.call_tool("add_subscription", {"url": "https://round.example/feed"})
        summary = await client.call_tool("fetch_feed", {})
    assert summary.structured_content["ok"] == 1

    round_spans = [s for s in span_exporter if s.name == "rss.refresh_all"]
    assert len(round_spans) == 1
    attributes = dict(round_spans[0].attributes)
    assert attributes["rss.round.total"] == 1
    assert attributes["rss.round.ok"] == 1
    assert attributes["rss.round.upserted"] == 0


def test_setup_telemetry_missing_extra_degrades_to_false(monkeypatch, caplog):
    import builtins
    import importlib

    from rss_mcp.telemetry import setup_telemetry

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter") or name.startswith("opentelemetry.sdk"):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    importlib.reload(importlib.import_module("rss_mcp.telemetry"))
    settings = _settings(otel_otlp_endpoint="http://127.0.0.1:43210/v1/traces")
    with caplog.at_level("WARNING"):
        assert setup_telemetry(settings) is False
    assert "otel extra is not installed" in caplog.text


def test_setup_telemetry_registers_provider():
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        from pathlib import Path
        from rss_mcp.config import Settings
        from rss_mcp.telemetry import setup_telemetry, shutdown_telemetry
        s = Settings(db_path=Path("/tmp/otel-test.db"), user_agent="t",
                     otel_otlp_endpoint="http://127.0.0.1:43210/v1/traces")
        ok = setup_telemetry(s)
        from opentelemetry import trace
        print(ok, type(trace.get_tracer_provider()).__name__)
        shutdown_telemetry()
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "True TracerProvider" in out.stdout


def test_load_settings_otel_endpoint(monkeypatch):
    from rss_mcp.config import load_settings

    monkeypatch.setenv("RSS_MCP_OTEL_OTLP_ENDPOINT", " http://obs-tempo:43210/v1/traces ")
    assert load_settings().otel_otlp_endpoint == "http://obs-tempo:43210/v1/traces"
    monkeypatch.setenv("RSS_MCP_OTEL_OTLP_ENDPOINT", "   ")
    assert load_settings().otel_otlp_endpoint is None