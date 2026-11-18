from pathlib import Path

from rss_mcp.config import load_settings

DEFAULT_USER_AGENT = "rss-mcp/0.1 (+https://github.com/...)"


def test_load_settings_respects_rss_mcp_db_env(tmp_path, monkeypatch):
    db = tmp_path / "custom.db"
    monkeypatch.setenv("RSS_MCP_DB", str(db))
    s = load_settings()
    assert s.db_path == db


def test_load_settings_defaults_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("RSS_MCP_DB", raising=False)
    monkeypatch.delenv("RSS_MCP_USER_AGENT", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    s = load_settings()
    assert s.db_path == tmp_path / ".local" / "share" / "rss-mcp" / "rss.db"
    assert s.user_agent == DEFAULT_USER_AGENT
    assert s.default_limit == 20
    assert s.max_limit == 100
    assert s.fetch_entry_cap == 50


def test_load_settings_respects_rss_mcp_user_agent_env(monkeypatch):
    monkeypatch.setenv("RSS_MCP_USER_AGENT", "custom-agent/9")
    s = load_settings()
    assert s.user_agent == "custom-agent/9"
