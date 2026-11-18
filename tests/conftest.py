from __future__ import annotations

from pathlib import Path

import pytest

from rss_mcp.config import Settings


@pytest.fixture
def server_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "rss" / "rss.db",
        user_agent="rss-mcp/test",
    )
