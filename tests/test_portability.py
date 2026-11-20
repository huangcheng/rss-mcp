"""JSONL export/import: round trips, merge, replace, and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rss_mcp import server as server_module
from rss_mcp.config import Settings
from rss_mcp.db import connect, init_db
from rss_mcp.models import EntryInput
from rss_mcp.portability import export_lines, import_lines
from rss_mcp.store import Store


def _settings(path: Path) -> Settings:
    return Settings(db_path=path / "rss.db", user_agent="rss-mcp/test")


def _open(settings: Settings) -> tuple[object, Store]:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn, Store(conn)


def _populate(settings: Settings) -> None:
    conn, store = _open(settings)
    news = store.add_subscription("https://news.example/feed", title="News", tags=["news"])
    tech = store.add_subscription(
        "https://tech.example/feed", title="Tech", tags=["tech"], muted=True, fetch_interval=900
    )
    store.upsert_entries(
        news.id,
        [
            EntryInput(
                guid="shared",
                url="https://news.example/a",
                title="Shared story",
                summary="syndicated",
                content="<p>full body</p>",
                published_at="2026-11-01T00:00:00Z",
            )
        ],
    )
    # Same canonical article in a second feed: exercises duplicate_of remapping.
    store.upsert_entries(
        tech.id,
        [
            EntryInput(guid="copy", url="https://news.example/a", title="Shared story"),
            EntryInput(guid="tech-only", title="Tech only", published_at="2026-11-02T00:00:00Z"),
        ],
    )
    # Per-subscription queries: aggregate views hide muted feeds, and this
    # fixture needs to reach into one.
    def _one(subscription_id: int, guid: str):
        rows = store.list_entries(
            subscription_id=subscription_id, include_duplicates=True, limit=10
        )
        return next(row for row in rows if row.guid == guid)

    store.mark_entries([_one(news.id, "shared").id], read=True)
    store.set_starred([_one(tech.id, "tech-only").id], starred=True)
    conn.close()


def test_export_import_round_trip_preserves_library(tmp_path):
    source = _settings(tmp_path / "source")
    source.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(source)
    conn, _ = _open(source)
    lines = list(export_lines(conn))
    conn.close()

    assert json.loads(lines[0])["type"] == "rss-mcp-export"
    assert json.loads(lines[0])["engine"] == "sqlite"
    assert sum(1 for line in lines if json.loads(line)["type"] == "entry") == 3

    target = _settings(tmp_path / "target")
    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn, store = _open(target)
    summary = import_lines(conn, lines)
    conn.close()

    assert summary["subscriptions_added"] == 2
    assert summary["entries_added"] == 3
    assert summary["states_restored"] == 2
    assert summary["duplicates_relinked"] == 1

    conn, store = _open(target)
    subs = {sub.url: sub for sub in store.list_subscriptions()}
    assert subs["https://tech.example/feed"].muted is True
    assert subs["https://tech.example/feed"].fetch_interval == 900
    assert subs["https://news.example/feed"].tags == ["news"]
    rows = [
        entry
        for sub in store.list_subscriptions()
        for entry in store.list_entries(
            subscription_id=sub.id, include_duplicates=True, limit=10
        )
    ]
    entries = {entry.guid: entry for entry in rows}
    assert entries["shared"].content == "<p>full body</p>"
    assert entries["shared"].read_at is not None
    assert entries["tech-only"].starred is True
    assert entries["copy"].duplicate_of == entries["shared"].id
    conn.close()


def test_import_merges_by_url_and_guid_without_duplicating(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, _ = _open(settings)
    lines = list(export_lines(conn))
    conn.close()

    conn, store = _open(settings)
    summary = import_lines(conn, lines)
    conn.close()

    assert summary["subscriptions_added"] == 0
    assert summary["subscriptions_matched"] == 2
    assert summary["entries_added"] == 0
    assert summary["entries_skipped"] == 3


def test_import_replace_clears_the_library_first(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, _ = _open(settings)
    lines = list(export_lines(conn))
    store = Store(conn)
    store.add_subscription("https://doomed.example/feed")
    conn.commit()
    conn.close()

    conn, store = _open(settings)
    summary = import_lines(conn, lines, replace=True)
    urls = {sub.url for sub in store.list_subscriptions()}
    conn.close()

    assert "https://doomed.example/feed" not in urls
    assert urls == {"https://news.example/feed", "https://tech.example/feed"}
    assert summary["entries_added"] == 3


def test_export_without_content_and_since_filter(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, _ = _open(settings)
    lines = list(export_lines(conn, since="2026-11-01T12:00:00Z", include_content=False))
    conn.close()

    entries = [json.loads(line) for line in lines if json.loads(line)["type"] == "entry"]
    # "copy" has no published_at, so the COALESCE fallback on fetched_at keeps
    # it in a --since backup; "shared" is dated before the cut and drops out.
    assert {entry["guid"] for entry in entries} == {"copy", "tech-only"}
    assert all(entry["content"] is None for entry in entries)


def test_import_rejects_foreign_and_newer_files(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn, _ = _open(settings)

    with pytest.raises(ValueError, match="format header"):
        import_lines(conn, ['{"hello": "world"}'])
    with pytest.raises(ValueError, match="newer"):
        import_lines(
            conn, [json.dumps({"type": "rss-mcp-export", "version": 99})]
        )
    with pytest.raises(ValueError, match="references subscription"):
        import_lines(
            conn,
            [
                json.dumps({"type": "rss-mcp-export", "version": 1}),
                json.dumps(
                    {
                        "type": "entry",
                        "id": 1,
                        "subscription_id": 7,
                        "guid": "x",
                        "fetched_at": "2026-11-01T00:00:00Z",
                    }
                ),
            ],
        )
    conn.close()


def test_cli_export_and_import_round_trip(tmp_path, monkeypatch, capsys):
    source = _settings(tmp_path / "cli-source")
    source.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(source)
    dump = tmp_path / "dump.jsonl"
    monkeypatch.setenv("RSS_MCP_DB", str(source.db_path))

    server_module.main(["export-db", "--out", str(dump)])
    summary = json.loads(capsys.readouterr().err)
    assert summary["export"] == {"subscriptions": 2, "entries": 3}
    assert sum(1 for _ in dump.open()) == 6  # header + 2 subscriptions + 3 entries

    target = _settings(tmp_path / "cli-target")
    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RSS_MCP_DB", str(target.db_path))
    server_module.main(["import-db", "--in", str(dump)])
    printed = json.loads(capsys.readouterr().out)
    assert printed["import"]["entries_added"] == 3

def test_failed_replace_import_leaves_the_library_intact(tmp_path):
    """The wipe and the load are one transaction: a bad file restores nothing."""
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, store = _open(settings)
    lines = list(export_lines(conn))
    conn.close()

    truncated = lines[:3] + ['{"type": "entry", "id": 99']
    conn, store = _open(settings)
    with pytest.raises(ValueError, match="invalid JSON"):
        import_lines(conn, truncated, replace=True)
    conn.rollback()
    conn.close()

    conn, store = _open(settings)
    urls = {sub.url for sub in store.list_subscriptions()}
    entries = [
        entry
        for sub in store.list_subscriptions()
        for entry in store.list_entries(
            subscription_id=sub.id, include_duplicates=True, limit=10
        )
    ]
    conn.close()
    assert urls == {"https://news.example/feed", "https://tech.example/feed"}
    assert len(entries) == 3


def test_merge_never_clears_read_or_star_state(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, store = _open(settings)
    lines = list(export_lines(conn))
    # Progress made after the backup: read the shared entry, star tech-only.
    sub_ids = {sub.url: sub.id for sub in store.list_subscriptions()}
    shared = next(
        entry
        for entry in store.list_entries(subscription_id=sub_ids["https://news.example/feed"], limit=10)
        if entry.guid == "shared"
    )
    tech_only = next(
        entry
        for entry in store.list_entries(subscription_id=sub_ids["https://tech.example/feed"], limit=10)
        if entry.guid == "tech-only"
    )
    store.mark_entries([shared.id], read=True)
    store.set_starred([tech_only.id], starred=True)
    conn.close()

    conn, store = _open(settings)
    import_lines(conn, lines)
    conn.close()

    conn, store = _open(settings)
    shared = store.get_entry(shared.id)
    tech_only = store.get_entry(tech_only.id)
    conn.close()
    assert shared is not None and shared.read_at is not None
    assert tech_only is not None and tech_only.starred is True


def test_replace_restores_state_verbatim(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    conn, store = _open(settings)
    lines = list(export_lines(conn))

    # A fresh target keeps whatever the file says, including an unread state.
    sub_ids = {sub.url: sub.id for sub in store.list_subscriptions()}
    shared = next(
        entry
        for entry in store.list_entries(subscription_id=sub_ids["https://news.example/feed"], limit=10)
        if entry.guid == "shared"
    )
    store.mark_entries([shared.id], read=False)
    conn.close()

    conn, store = _open(settings)
    import_lines(conn, lines, replace=True)
    conn.close()

    conn, store = _open(settings)
    restored = next(
        entry
        for sub in store.list_subscriptions()
        for entry in store.list_entries(subscription_id=sub.id, limit=10)
        if entry.guid == "shared"
    )
    conn.close()
    assert restored.read_at is not None


def test_cli_export_normalizes_since_and_rejects_garbage(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _populate(settings)
    monkeypatch.setenv("RSS_MCP_DB", str(settings.db_path))

    dump = tmp_path / "since.jsonl"
    # +08:00 offset: text comparison would place this after "2026-11-02T00:00:00Z"
    # and export nothing; the normalized instant is 2026-11-01T23:00Z.
    server_module.main(
        ["export-db", "--out", str(dump), "--since", "2026-11-02T07:00:00+08:00"]
    )
    capsys.readouterr()
    guids = {
        json.loads(line)["guid"]
        for line in dump.read_text().splitlines()
        if json.loads(line)["type"] == "entry"
    }
    assert "tech-only" in guids

    with pytest.raises(SystemExit) as excinfo:
        server_module.main(["export-db", "--out", str(dump), "--since", "not-a-date"])
    assert excinfo.value.code != 0


def test_import_reports_missing_fields_with_a_line_number(tmp_path):
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn, _ = _open(settings)

    with pytest.raises(ValueError, match="line 2: subscription record is missing required field 'url'"):
        import_lines(
            conn,
            [
                json.dumps({"type": "rss-mcp-export", "version": 1}),
                json.dumps({"type": "subscription", "id": 1, "title": "no url"}),
            ],
        )
    conn.close()
