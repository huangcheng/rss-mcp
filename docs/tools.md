# 工具参考

15 个工具、2 个资源、1 个提示词。签名以 [`freeze-manifest.json`](./freeze-manifest.json) 为准
（CI 会校验它和运行中的 server 一致）；本页讲"怎么用"。

约定：`*` = 必填。所有时间参数接受 ISO 8601，内部统一存成 `YYYY-MM-DDTHH:MM:SSZ` 并按字符串比较，
带时区偏移的写法会在入口归一化（`2026-11-02T07:00:00+08:00` → `2026-11-01T23:00:00Z`）。
列表类工具有 `limit` 上限（默认 20，最大 100，可由部署方收窄）。

## 订阅管理

| 工具 | 参数 |
|---|---|
| `add_subscription` * | `url*`, `title`, `tags`, `user_agent`, `headers`, `muted`, `include_keywords`, `exclude_keywords`, `fetch_interval` |
| `update_subscription` * | `subscription_id*`, `title`, `tags`, `user_agent`, `headers`, `disabled`, `muted`, `include_keywords`, `exclude_keywords`, `fetch_interval` |
| `remove_subscription` * | `subscription_id*` |
| `list_subscriptions` | `tag`, `failing_only` |
| `discover_feed` * | `url*` |

- `headers` 是字符串到字符串的映射，用于每源鉴权（`Authorization`、`Cookie` 等），只在抓取该源时使用。
- `include_keywords` / `exclude_keywords` 是大小写不敏感的包含匹配；被 `exclude_keywords` 命中的条目
  记为 `filtered=1`，在所有列表/摘要/搜索里都不出现（保留是为了让规则变了能重新判断）。
- `muted` 的源不参与聚合视图（digest、list_entries 不带 `subscription_id` 时），但按 id 查仍然可见。
- `fetch_interval` 是每源最短抓取间隔（秒），0/未设表示跟随全局；上限 2147483647。
- `failing_only=true` 返回 `last_error` 非空的源，并附 `_remediation_hint`（先查 TLS，再按 401/403/404/429/5xx 给建议）。
- `disabled` 的源不会被任何自动流程抓取（Temporal 轮次和进程内循环都会跳过），只有 `fetch_feed(subscription_id=…)`
  这种显式按 id 的调用会抓取并重新启用它 —— 这是刻意的恢复通道。

`rss://subscriptions` 资源返回与 `list_subscriptions` 相同的数据；任何订阅变更都会推送
`notifications/resources/updated`。

## 抓取

| 工具 | 参数 |
|---|---|
| `fetch_feed` | `subscription_id` |
| `server_status` | — |

- `fetch_feed()` 不带 id = 抓取全部（受 `disabled` / `fetch_interval` / `ttl` / `skipHours`/`skipDays` 门控，
  返回里 `skipped` 计数和每源 `reason` 说明为什么跳过）；带 id = 只抓那一个，忽略间隔门控。
- `server_status()` 返回 `database`（引擎与计数）、`feeds`（ok/failing/disabled/muted/never_fetched/empty）、
  `fetching`、`retention`（含上次 prune 与累计删除）、`reading`（pending/delivered_24h/by_tag）、
  `failing_feeds`、`warnings`。**`warnings` 为空才代表没有已知问题**。

## 阅读

| 工具 | 参数 |
|---|---|
| `list_entries` | `subscription_id`, `unread_only`, `limit`, `since`, `include_duplicates`, `tag`, `starred` |
| `search_entries` * | `query*`, `subscription_id`, `unread_only`, `limit`, `include_duplicates`, `tag`, `since` |
| `read_entry` * | `entry_id*`, `fetch_full`, `mark_read` |
| `get_digest` | `since`, `tag`, `max_entries`, `per_feed`, `include_delivered`, `replay_days`, `mark_read` |
| `mark_entry` | `entry_ids`, `subscription_id`, `read`, `tag`, `before`, `all` |
| `star_entry` * | `entry_ids*`, `starred` |

- `list_entries` / `search_entries` 默认不含正文（`content=None`），正文只在 `read_entry` 返回。
- `include_duplicates=false`（默认）隐藏跨源重复的副本；副本通过 `duplicate_of` 指向保留的那条。
- `get_digest` 是主入口：按源取 `per_feed` 条、组装时去重、默认跳过**之前已经投递过**的条目。
  - `include_delivered=true` 关掉投递过滤（重看全部）
  - `replay_days=N` 只重发"上次投递早于 N 天前"的条目
  - `mark_read=true` 把本次返回的标记已读（**星标条目不动**），并推送 unread 资源更新
  - 响应里的 `delivered` / `marked_read` / `pending` 分别是本次条数、本次标记数、剩余未投递未读数
- `mark_entry` 的批量清扫（`tag` / `before` / `all`）**不碰星标条目**；要改星标条目必须用 `entry_ids`。
- `read_entry(fetch_full=true)` 会在线抓原文并用 readability 抽正文；`mark_read=true` 顺手标记已读。

`rss://entries/unread` 返回未读列表；读状态变化（`read_entry` / `mark_entry` / `get_digest(mark_read=true)`）
会推送更新。

## 导入导出

| 工具 | 参数 |
|---|---|
| `import_opml` * | `opml*` |
| `export_opml` | — |

- `import_opml` 把 OPML 的 folder 名转成 tag，已存在的 URL 跳过。
- `export_opml` 输出可直接导入其他阅读器的 OPML。
- 整库（含已读/星标、投递状态、去重链接）用 CLI：`rss-mcp export-db` / `import-db`，
  见 [deployment.md](./deployment.md#备份与恢复)。

## 提示词

`daily-digest` —— 一段可直接喂给宿主的提示词，让模型调 `get_digest` 并总结当天内容。