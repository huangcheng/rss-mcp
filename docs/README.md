# rss-mcp 文档

给宿主（agent）用的 RSS 服务器：单一职责是"把订阅抓进来、按你需要的样子交出去"。
没有 GUI、没有多用户、没有内置模型 —— 摘要和判断由宿主完成。

## 从这里开始

- **[工具参考](./tools.md)** —— 15 个工具的参数与语义、两个资源的推送时机
- **[部署](./deployment.md)** —— stdio/Docker/PostgreSQL/Temporal、备份恢复、环境变量
- **[可观测性](./observability.md)** —— 日志、traces、`/metrics`、`server_status`、参考 dashboard
- **[稳定性政策](./stability.md)** —— 1.0 冻结面、破坏性变更定义、弃用流程、soak gate

## 三十秒版本

```bash
# 本机 stdio
uvx rss-mcp-server

# 远程 HTTP（单 token 保护）
docker run -d --name rss-mcp -p 127.0.0.1:8000:8000 \
  -e RSS_MCP_AUTH_TOKEN=<bearer> -v rss-data:/data rss-mcp-server:0.5.1
```

然后在宿主里连上 `http://127.0.0.1:8000/mcp`（`Authorization: Bearer <bearer>`），先调
`server_status` 看健康，再用 `add_subscription` / `import_opml` 加源，最后 `get_digest` 开始读。

## 设计取舍（读文档前先知道这四条）

1. **单用户、无鉴权体系**：一个（或一组）bearer token，没有账号、没有 OAuth、没有按用户分库。
2. **agent-only**：所有输出都是结构化 JSON；不生成 HTML 页面，不做邮件/推送 —— 宿主负责呈现。
3. **索引是缓存，不是归档**：`RSS_MCP_RETENTION_DAYS` 到期的条目会被删除（星标除外）；
   需要长期保存就导出（`export-db`）或调大保留期。
4. **礼貌抓取**：遵循 `ttl` / `skipHours` / `skipDays`、每源最短间隔、条件请求（ETag/Last-Modified）、
   全局并发上限。不做 robots.txt 检查（feed URL 本身即为授权），会被反爬拦截的源建议直接删掉。

## 目录约定

```
src/rss_mcp/         服务端（server.py 是工具层，store/dialect 是存储层）
scheduler/           Temporal worker（可选的抓取驱动）
deploy/temporal/     Temporal 私有部署 compose
docs/                本目录
scripts/             uvx 烟雾测试、freeze 清单更新
tests/               单元 + PostgreSQL 集成 + 真网络样本
```