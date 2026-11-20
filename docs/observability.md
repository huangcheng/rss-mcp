# 可观测性

三层，各管一件事：**日志**说发生了什么，**traces** 说一次调用经过了哪里、多慢，
**指标**说库里现在是什么状态（有没有堆积、有没有停摆）。

## 1. 日志（默认开）

- 默认人类可读行；`RSS_MCP_LOG_FORMAT=json` 切到每行一个 JSON 对象（写 stderr）。
- 字段：`ts`（ISO 8601 UTC）、`level`、`logger`、`msg`；HTTP 请求附带 `http_method`/`http_path`/
  `mcp_method`/`mcp_name`；装了 OTel 时再附 `trace_id`/`span_id`。
- 常见查法：

```bash
docker logs --since 1h rss-mcp | jq -c 'select(.level=="ERROR")'
docker logs rss-mcp | jq -c 'select(.msg|test("prune"))'
docker logs rss-mcp | jq -c 'select(.trace_id=="<id>")'   # 与 Tempo 里的 trace 对齐
```

## 2. Traces（可选，需要 `[otel]` extra）

```bash
pip install 'rss-mcp-server[otel]'
RSS_MCP_OTEL_OTLP_ENDPOINT=http://tempo:4318/v1/traces
```

- MCP SDK 的 `OpenTelemetryMiddleware` 给每个请求建 span（`gen_ai.*` 语义），并解析 W3C `traceparent`。
- 自定义 span：`rss.feed_fetch`（属性 `rss.subscription.id`、`rss.feed.host`；**不记录完整 URL**，
  避免把 URL 里的凭证写进 trace）、`rss.refresh_all`（属性 `rss.round.*`）。
- 关闭时零开销：没有 endpoint 就不装 provider。

## 3. 指标（`/metrics`，需设 token）

`RSS_MCP_METRICS_TOKEN` 未设置时路由返回 404；设置后需 `Authorization: Bearer <token>`。
格式是 Prometheus 文本；每次抓取单独开连接，不参与请求路径的事务。

| 指标 | 类型 | 含义 |
|---|---|---|
| `rss_mcp_up` | gauge | 1 = 本次抓取数据库可读（失败时整页降级为 `up 0`） |
| `rss_mcp_info{version,engine}` | gauge | 版本与引擎 |
| `rss_mcp_subscriptions{state}` | gauge | 互斥健康桶：`ok`/`failing`/`disabled`/`muted`/`never_fetched`，**求和 = 订阅总数** |
| `rss_mcp_feeds_empty` | gauge | 叠加指标：最近一次解析成功的抓取返回 0 条的源（`NULL`=还没抓过，不算空） |
| `rss_mcp_entries{state}` | gauge | `total`/`unread`/`starred`/`filtered`/`duplicates_hidden` |
| `rss_mcp_feed_last_success_timestamp_seconds{host}` | gauge | 每 host 最近成功时间 |
| `rss_mcp_feed_consecutive_failures{host}` | gauge | 每 host 连续失败数之和 |
| `rss_mcp_last_successful_fetch_timestamp_seconds` | gauge | 全局最近成功抓取 |
| `rss_mcp_refresh_interval_seconds` | gauge | 进程内间隔；0 = 外部驱动 |
| `rss_mcp_retention_days` | gauge | 保留天数；0 = 未启用 |
| `rss_mcp_prune_last_run_timestamp_seconds` | gauge | 上次 prune（含空转）时间；没跑过则不出现在输出里 |
| `rss_mcp_prune_removed_total` | counter | 累计删除条目数 |
| `rss_mcp_digest_pending` | gauge | 可见未读且尚未被 digest 投递过的条目 |
| `rss_mcp_digest_delivered_24h` | gauge | 24h 内投递条数 |
| `rss_mcp_tag_pending{tag}` | gauge | 按标签的待读（muted 不计，disabled 仍计） |
| `rss_mcp_status_warnings` | gauge | `server_status.warnings` 条数；**告警看这个 > 0** |

指标名与标签是 1.0 冻结面的一部分，见 [stability.md](./stability.md)。

## 自检（比指标更适合人看）

`server_status` 工具和 `rss-mcp status` 共用一份 JSON：

```json
{
  "database": {"engine": "postgres", "subscriptions": 79, "entries": 5767, "unread": 5767,
               "starred": 0, "filtered": 0, "duplicates_hidden": 0},
  "feeds": {"ok": 79, "failing": 0, "disabled": 0, "muted": 0, "never_fetched": 0,
            "total": 79, "empty": 0},
  "fetching": {"driver": "external-or-manual", "last_successful_fetch_at": "...", "...": "..."},
  "retention": {"retention_days": 90, "last_prune_at": "...", "last_prune_removed": 0,
                "prune_total_removed": 0, "seconds_since_last_prune": 812},
  "reading": {"pending": 5767, "delivered_24h": 0, "by_tag": {"购物": 3705, "...": 0}},
  "failing_feeds": [],
  "warnings": []
}
```

`warnings` 覆盖已真实踩过的静默故障：设置了保留期但 prune 从未运行/超过 3h 没跑、
24h 没有任何成功抓取、库存在一天以上却从未抓取成功、有 failing 源、有抓取成功但零条目的源。
**刻意暂停的源不是警告。**

## 参考 dashboard 与告警（Grafana）

`grafana.wuhan.dev/d/rss-mcp/rss-mcp` 的十来个面板分三组：

- **流量**（Tempo span metrics）：请求速率按 span 名、Feed 抓取延迟 p50/p95/p99、结果分布（`status_code`）
- **状态**（`/metrics`）：服务在线、未读、条目总数、失败/禁用源、距上次 prune
- **消费**：待读（未投递）、24h 已投递、空源、按标签待读

四条告警规则：

| 条件 | 含义 |
|---|---|
| `rss_mcp_up < 1` | 数据库打不开或 `/metrics` 不可达 |
| `rss_mcp_status_warnings > 0` | 库自检报告了问题（看 `warnings`） |
| `(time() - rss_mcp_prune_last_run_timestamp_seconds) > 21600` | prune 停摆超过 6h（0.3.5 那类静默失效） |
| `(rss_mcp_digest_pending > 500) and (rss_mcp_digest_delivered_24h == 0)` | 只进不出：待读堆积且一天没有投递 |

> 说明：Grafana 11.5.1 的 Tempo 数据源**不支持面板内 TraceQL 检索**（`backend TraceQL search queries
> are not supported`）。dashboard 里用"Explore 深链 + span-metrics 表格"替代 Trace 列表，这是 Grafana
> 的版本限制，不是数据问题。

## 已知的观测盲点

- 指标是**抓取时**的状态，看不到两次抓取之间的瞬时尖峰；需要时看 traces。
- `duplicates_hidden` 只统计有 `duplicate_of` 的副本；跨源同 URL 但没有共同 keeper 的情况不计。
- 日志轮转交给容器运行时（`json-file`，10MB × 3）；长期留存需要外部收集。