# 部署

## 三种形态

| 形态 | 适用 | 存储 | 入口 |
|---|---|---|---|
| `uvx rss-mcp-server` | 单机、宿主直接拉起 stdio | SQLite | 无网络端口 |
| Docker + streamable HTTP | 远程/多客户端 | SQLite 或 PostgreSQL | `http://host:8000/mcp` |
| Docker + Temporal | 需要跨重启的定时抓取 | PostgreSQL | 同上 + worker |

## stdio（uvx）

```bash
uvx rss-mcp-server                     # 默认 transport=stdio
RSS_MCP_DB=~/rss.db uvx rss-mcp-server
```

宿主里的 MCP 配置：

```json
{ "mcpServers": { "rss": { "command": "uvx", "args": ["rss-mcp-server"] } } }
```

stdout 是协议通道，日志一律走 stderr —— 所以 `RSS_MCP_LOG_FORMAT=json` 在 stdio 下也安全。

## Docker / HTTP

```bash
docker run -d --name rss-mcp -p 127.0.0.1:8000:8000 \
  -e RSS_MCP_AUTH_TOKEN=<bearer> \
  -v rss-data:/data \
  rss-mcp-server:0.5.1 serve --transport streamable-http --host 0.0.0.0 --port 8000
```

- 镜像内 `ENTRYPOINT ["rss-mcp"]`、`CMD ["serve", "--transport", "streamable-http", ...]`，
  所以 `docker run <image> status` 可以直接跑自检。
- 鉴权：`RSS_MCP_AUTH_TOKEN`（单 token）或 `RSS_MCP_AUTH_TOKENS`（逗号分隔多 token，便于轮换）。
  未设置时**任何请求都会被拒**，不会退化成匿名。
- 反向代理只需转发 `POST/GET/DELETE /mcp` 与 `GET /healthz`；`/metrics` 建议只在内网暴露。
- 协议行为：同一端点上同时支持 2026-07-28（无状态、每请求一个 POST）与 ≤2025-11-25（initialize 握手），
  由请求头/`_meta` 自动判定。

## PostgreSQL

```bash
RSS_MCP_DB_URL=postgresql://rss:pass@postgres:5432/rss
```

- `RSS_MCP_DB_URL` 优先于 `RSS_MCP_DB`；支持 `postgresql://`、`postgres://`、`sqlite:///绝对路径`。
- 首次启动自动建表 + 迁移；搜索用 `tsvector` + `rss_unaccent`（有 `unaccent` 扩展时折叠变音符，
  没有则退回 `simple`）。
- 从 SQLite 迁移：`rss-mcp export-db --out x.jsonl` → 指向 PG → `rss-mcp import-db --in x.jsonl`。

## 定时抓取

两种驱动，二选一：

1. **进程内**：`RSS_MCP_REFRESH_INTERVAL=900`（秒）。适合单容器。
2. **Temporal**（推荐，跨重启不丢）：`deploy/temporal/` 里是私有部署的 compose。
   - worker 调 MCP HTTP 端点，按 `RefreshAllWorkflow` 扇出每个源一个 child workflow
   - 重试策略 1m→1h、最多 5 次；`OverlapPolicy=Skip` 防止轮次堆叠
   - 轮次间隔在 Temporal schedule 上配置，服务端不再需要 `RSS_MCP_REFRESH_INTERVAL`

无论哪种驱动，`disabled` 源都不会被自动抓取；`fetch_interval` / `ttl` / `skipHours`/`skipDays` 在两层都生效。

## 备份与恢复

```bash
# 每日 03:30（cron）
docker exec rss-postgres pg_dump -U rss rss | gzip > /var/backups/rss-mcp/rss-$(date +%Y%m%d-%H%M%S).sql.gz
# 保留 14 份
ls -1t /var/backups/rss-mcp/rss-*.sql.gz | tail -n +15 | xargs -r rm -f
```

**必须定期做恢复演练**（`/etc/rss-mcp/verify-backup.sh`，每周日 04:30）：

1. `gzip -t` 校验
2. 建临时库 `rss_restore_check`
3. 恢复并检查退出码、日志里没有 `ERROR/FATAL`
4. 校验订阅/条目非空、无孤立行（entries / entry_state / digest_deliveries 三处外键）
5. 无论成败都 drop 临时库；结果以 JSON 行写 `/var/log/rss-mcp/verify-backup.log`

> 2026-11-20 实测：5.2MB dump 恢复后 79 订阅 / 5767 条目，与生产一致，0 孤立行。

便携导出（跨引擎/跨机器）：

```bash
rss-mcp export-db --out backup.jsonl            # 含已读/星标/投递状态/去重链接
rss-mcp export-db --out small.jsonl --no-content
rss-mcp import-db --in backup.jsonl             # 按 URL/guid 合并，幂等
rss-mcp import-db --in backup.jsonl --replace   # 单事务清库重建；坏文件不会留下空库
```

## 升级

1. 拉代码/换镜像 → 重启容器（迁移在启动时自动执行，只加不减）
2. `curl /healthz` 看 `version`
3. `docker exec rss-mcp rss-mcp status` 看 `warnings` 是否为空
4. `curl -H "Authorization: Bearer $RSS_MCP_METRICS_TOKEN" /metrics | grep rss_mcp_up`
5. 首个 prune 周期（部署后 1 小时）之后确认 `rss_mcp_prune_last_run_timestamp_seconds` 前进

## CLI

```
rss-mcp [serve] [--transport stdio|streamable-http] [--host H] [--port P] [--path /mcp]
rss-mcp status                 # 与 server_status 工具同一份 JSON
rss-mcp export-db --out -      # stdout；--since ISO；--no-content
rss-mcp import-db --in -       # stdin；--replace
```

`serve` 是默认子命令，所以历史写法 `rss-mcp --transport streamable-http …` 仍然有效。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `RSS_MCP_DB` | `~/.local/share/rss-mcp/rss.db` | SQLite 路径 |
| `RSS_MCP_DB_URL` | 空 | 优先于 `RSS_MCP_DB`；postgres 或 `sqlite:///` |
| `RSS_MCP_AUTH_TOKEN` | 空 | HTTP bearer；空 = 拒绝所有请求 |
| `RSS_MCP_AUTH_TOKENS` | 空 | 逗号分隔多 token（轮换用） |
| `RSS_MCP_USER_AGENT` | `rss-mcp/<version> (+repo)` | 抓取 UA（每源可覆盖） |
| `RSS_MCP_FETCH_CONCURRENCY` | `8` | 并发抓取数 |
| `RSS_MCP_REFRESH_INTERVAL` | 空 | 进程内抓取间隔（秒）；Temporal 模式下不设 |
| `RSS_MCP_DISABLE_AFTER_FAILURES` | `5` | 连续失败多少次自动禁用（0 = 不自动禁用） |
| `RSS_MCP_RETENTION_DAYS` | 空 | 保留天数；设置后每小时 prune 一次（星标永不删） |
| `RSS_MCP_RATE_LIMIT_PER_MINUTE` | `120` | 每 token/IP 每分钟请求数（0 = 关闭） |
| `RSS_MCP_OTEL_OTLP_ENDPOINT` | 空 | OTLP/HTTP traces 端点；需要 `[otel]` extra |
| `RSS_MCP_METRICS_TOKEN` | 空 | `/metrics` 的 bearer；空 = 路由 404 |
| `RSS_MCP_LOG_FORMAT` | 空 | 设为 `json` 输出 JSON Lines 日志 |