# rss-mcp Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the post-v1 gaps that hurt agent UX and release hygiene: agent-sized list/search payloads, optional mark-read on read, SDK/config robustness, PyPI metadata, and tracked live OPML tests.

**Architecture:** Keep the existing Store/server split. Prefer shaping MCP tool responses in `server.py` (omit/truncate fields for list/search) rather than changing Store row shapes. Harden config + feed_client at the edges. No new transports or UI.

**Tech Stack:** Existing (`mcp` SDK v2, httpx, feedparser, pytest). Manual PyPI publish only — no GitHub Actions release workflow.

**Spec:** `docs/superpowers/specs/2026-11-18-rss-mcp-design.md` (agent-sized results, agent-only, MCP 2026-07-28)

## Global Constraints

- Product `rss-mcp` / import `rss_mcp`; stdio tools-only; no GUI/TUI
- MCP 2026-07-28 via official SDK; pin `mcp` to a compatible minor range (e.g. `>=2.3,<3`)
- Default DB `~/.local/share/rss-mcp/rss.db`; override `RSS_MCP_DB`
- Agent-sized defaults remain: list/search default limit 20, max 100
- `read_entry` remains the only tool that returns full article bodies by default
- No auto-release CI in this plan (manual `uv publish`)
- Commits only when the human asks (plan may say “commit”; skip unless requested)

## Review Focus

- `list_entries` / `search_entries` after a feed with large HTML `content` → response omits or truncates body; `read_entry` still returns full content
- `read_entry(..., mark_read=True)` → `read_at` set; default `mark_read=False` leaves unread unchanged
- `RSS_MCP_DB=" "` → treated as unset (falls back to default path), not a relative `" "` path
- Non-ASCII `RSS_MCP_USER_AGENT` on `fetch_feed` → `FetchResult(status="error")`, never raises
- `server/discover` → `serverInfo.version` is `0.1.0` (or package `__version__`); tools capability without list_changed listen expectation if `subscriptions=False`

## File structure (touched)

```
pyproject.toml                         # metadata + mcp pin
LICENSE                                # MIT (or SPDX already chosen)
README.md                              # note list/search omit content; mark_read
src/rss_mcp/__init__.py                # version stays in sync with pyproject
src/rss_mcp/config.py                  # strip RSS_MCP_DB
src/rss_mcp/feed_client.py             # UnicodeEncodeError; optional to_thread parse
src/rss_mcp/server.py                  # entry payloads; mark_read; MCPServer kwargs
tests/test_config.py
tests/test_feed_client.py
tests/test_server_tools.py
tests/test_real_inoreader_opml.py      # already present untracked → keep skipif
```

---

### Task 1: PyPI / package metadata

**Files:**
- Modify: `pyproject.toml`
- Create: `LICENSE` (MIT)
- Modify: `README.md` (only if metadata links need a one-line “License” mention — optional)
- Test: none required beyond `uv build` succeeding

**Interfaces:**
- Produces: `readme = "README.md"`; `license = "MIT"`; `authors`; `[project.urls]` Homepage/Repository (use `https://github.com/huangcheng/rss-mcp` unless human provides another URL — Ruling recorded in PR/commit message if wrong)
- Produces: `dependencies` pin `mcp>=2.3,<3`
- Keeps `version = "0.1.0"` aligned with `rss_mcp.__version__`

- [ ] **Step 1: Add metadata + license file + mcp pin**
- [ ] **Step 2: Run `uv build` — expect wheel/sdist in `dist/`**
- [ ] **Step 3: Run `uv run pytest -q` — expect existing suite still green**

---

### Task 2: Config + fetch edge hardening

**Files:**
- Modify: `src/rss_mcp/config.py`
- Modify: `src/rss_mcp/feed_client.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_feed_client.py`

**Interfaces:**
- `load_settings`: `raw_db = os.environ.get("RSS_MCP_DB");` if missing or `not raw_db.strip()` → default path; else `Path(raw_db.strip()).expanduser()`
- `fetch_feed` except tuple includes `UnicodeEncodeError` → `_error(...)`
- Optional in same task: wrap `feedparser.parse(io.BytesIO(body))` in `await asyncio.to_thread(...)` so cancellation can land between HTTP and parse (keep path-injection BytesIO behavior)

- [ ] **Step 1: Failing tests** `test_load_settings_whitespace_rss_mcp_db_uses_default`, `test_fetch_feed_returns_error_for_non_ascii_user_agent`
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run focused + full suite — expect PASS**

---

### Task 3: Agent-sized list/search + mark_read on read

**Files:**
- Modify: `src/rss_mcp/server.py`
- Modify: `tests/test_server_tools.py`
- Modify: `README.md` (tool one-liners)

**Interfaces:**
- Add helper `_entry_payload(entry: Entry, *, full: bool) -> dict`:
  - `full=True` → `asdict(entry)` (read path)
  - `full=False` → `asdict(entry)` but set `content` to `None` (omit body; keep `summary`)
- `list_entries` / `search_entries` return `{"entries": [_entry_payload(e, full=False) for e in rows]}`
- `read_entry(entry_id, fetch_full: bool = False, mark_read: bool = False)`:
  - existing fetch_full behavior
  - if `mark_read`: `store.mark_entries([entry_id], True)` then re-get entry before return
  - return `_entry_payload(entry, full=True)`
- Do **not** change Store `list_entries`/`search_entries` return types

- [ ] **Step 1: Failing tests**
  - `test_list_entries_omits_content_bodies`
  - `test_search_entries_omits_content_bodies`
  - `test_read_entry_returns_full_content`
  - `test_read_entry_mark_read_default_false`
  - `test_read_entry_mark_read_true_sets_read_at`
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement helper + tool arg + README one-liner updates**
- [ ] **Step 4: Full suite PASS**

---

### Task 4: MCPServer identity + subscriptions off

**Files:**
- Modify: `src/rss_mcp/server.py`
- Modify: `tests/test_server_tools.py`

**Interfaces:**
- `MCPServer("rss-mcp", version=__version__, subscriptions=False, lifespan=...)`
- Keep existing private handler pop for resources/prompts until SDK offers a public API
- Assert discover/handshake: `server_info.version == "0.1.0"` (or whatever `__version__` is)
- Assert tools capability still present; if SDK exposes `list_changed`, prefer false / no listen when subscriptions=False — pin whatever the installed SDK actually reports and document in test name

- [ ] **Step 1: Failing test** for version + subscriptions behavior
- [ ] **Step 2: Implement MCPServer kwargs**
- [ ] **Step 3: Suite PASS**

---

### Task 5: Land live Inoreader OPML tests

**Files:**
- Add (if not already): `tests/test_real_inoreader_opml.py` (currently untracked)
- Modify: `README.md` — short “Live tests” note: path + `pytest tests/test_real_inoreader_opml.py -v -s`, skips if file missing

**Interfaces:** none new — keep `skipif(not OPML_PATH.is_file())`; do not require network in default CI mental model (document as optional)

- [ ] **Step 1: Ensure file is in repo tree with skipif**
- [ ] **Step 2: Offline: `uv run pytest -q` still passes (live tests skipped without file OR file present but mark network tests clearly)**
  - Ruling: keep tests collectable; they need network when not skipped — default local path skip is enough for machines without the OPML
- [ ] **Step 3: With OPML present + network, optional sanity: run the module once**

---

## Spec coverage

| Gap | Task |
|-----|------|
| Agent-sized list/search | 3 |
| read → optional mark_read | 3 |
| Pin mcp / PyPI metadata | 1 |
| version + subscriptions=False | 4 |
| RSS_MCP_DB strip / non-ASCII UA | 2 |
| Live OPML tests tracked | 5 |
| to_thread parse (optional) | 2 |
| GitHub auto-release | Out of scope (declined) |

## Out of scope

- GitHub Actions publish workflow
- HTTP/SSE transport, Resources/Prompts/Apps
- OPML folder→tags, URL canonicalization beyond strip
- Changing default DB layout / XDG_DATA_HOME
- Publishing to PyPI in this plan (human runs `uv publish` after)
