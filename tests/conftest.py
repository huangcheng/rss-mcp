from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from rss_mcp.config import Settings


@pytest.fixture
def server_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "rss" / "rss.db",
        user_agent="rss-mcp/test",
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url():
    """PostgreSQL test URL: RSS_MCP_TEST_PG_URL (CI service) or a throwaway
    Docker container. Skips when neither is available."""
    psycopg = pytest.importorskip("psycopg")
    env_url = os.environ.get("RSS_MCP_TEST_PG_URL")
    if env_url:
        yield env_url
        return
    if not shutil.which("docker"):
        pytest.skip("docker not available for PostgreSQL tests")
    port = _free_port()
    name = f"rss-mcp-test-pg-{port}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_DB=rss_test",
                "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        pytest.skip(f"docker not usable for PostgreSQL tests: {exc}")
    url = f"postgresql://postgres:test@127.0.0.1:{port}/rss_test"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            break
        except Exception:
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("PostgreSQL container did not become ready")
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)