# Stability policy（1.0 冻结草案）

状态：**candidate-freeze**（从 2026-11-20 起 soak，目标版本 1.0.0）

这份文档定义 1.0 之后"什么算破坏性变更"，以及变更必须走什么流程。机器可读的契约在
[`freeze-manifest.json`](./freeze-manifest.json)，由 `tests/test_freeze_manifest.py` 在 CI 中强制校验：
任何签名、资源、提示词、CLI、环境变量或指标的变化，如果没有同步更新清单，测试直接失败。

## 冻结面（1.0 之后只能按下面的流程改）

| 面 | 内容 | 清单字段 |
|---|---|---|
| MCP 工具 | 15 个工具的名称与完整 `inputSchema`（参数名、类型、是否必填、默认值、`anyOf` 结构） | `mcp.tools` |
| MCP 资源 | `rss://subscriptions`、`rss://entries/unread` 的 URI | `mcp.resources` |
| MCP 提示词 | `daily-digest` | `mcp.prompts` |
| CLI | 子命令 `serve`/`status`/`export-db`/`import-db`，`serve` 的四个参数，默认命令 `serve` | `cli` |
| 环境变量 | `RSS_MCP_*` 的名称与语义（值域变化不算破坏，重命名算） | `env` |
| 指标 | `rss_mcp_*` 指标族名称与标签键（新增指标不算破坏，重命名/删除算） | `metrics` |
| 数据库 schema | 表/列/索引名称；迁移只能加，不能不加迁移就改语义 | —（由迁移测试覆盖） |

**不在冻结面**：工具描述文案、日志文案、面板布局、内部模块结构、README 措辞。
它们可以随时改，不触发弃用流程。

## 破坏性变更的定义

- 删除或重命名工具/参数/资源/提示词/子命令/环境变量/指标
- 把可选参数变必填，或收窄类型（`str` → `int`、去掉 `anyOf` 分支）
- 改变已冻结响应的字段名或类型；删除字段
- 改变默认值从而使既有调用的含义变化（例如 `per_feed` 默认 3 → 0）

**以下不算破坏性**：新增工具、新增可选参数、新增响应字段、新增指标、新增环境变量、
放宽类型、纯文案修改。

## 弃用流程

1. **标注**：在参数/字段/工具的 docstring 与 README/docs 中标注 `deprecated since 0.x`，
   并说明替代方案。新增的能力同时提供。
2. **至少保留一个大版本**：`0.A` 标注 → 最早 `0.A+2` 才能移除（中间必须有一个完整的
   次版本发布，让宿主有机会升级）。
3. **记录**：在同一提交里更新 `docs/freeze-manifest.json`，并在 `ROADMAP.md` 的
   "Deprecations" 段落记一行（标的、替代品、计划移除版本）。
4. **移除**：删除的同时更新清单与文档；CI 的清单测试保证三处一致。

## 版本语义

- `0.x.y`：`x` 增加 = 新能力/行为变化；`y` 增加 = 修复与文档。
- `1.0.0` 之后：`MAJOR` 只用于上面定义的破坏性变更，`MINOR` 加能力，`PATCH` 只修 bug。
- MCP 协议版本追踪：`serverInfo.version` 跟随包版本；协议协商逻辑随 SDK 升级单独测试，
  协议升级不算本项目的破坏性变更。

## 1.0 的进入条件（soak gate）

全部满足才算达成：

1. 自 2026-11-20 起 **连续 14 天**没有 schema 迁移、没有冻结面变更（清单测试即为证据）。
2. 线上连续 14 天满足：`rss_mcp_up = 1`、`rss_mcp_status_warnings = 0`、无 failing/empty 源。
3. `verify-backup.sh` 至少成功运行 2 次（每周日），日志里 `status:"ok"`。
4. 至少 7 天里有实际的 digest 消费（`rss_mcp_digest_delivered_24h > 0` 至少一次），
   证明增量阅读闭环被真实使用过。
5. 文档站（`docs/`）覆盖工具参考、部署、可观测性三块，且与清单一致。

任何一条不满足就继续留在 0.x，不为了"发 1.0"而跳过。

## 变更清单的操作方式

```bash
# 改了冻结面之后：更新清单
uv run python scripts/update_freeze_manifest.py   # （见下）或用 tests 报错逐项手改
# 校验
uv run pytest tests/test_freeze_manifest.py -q
```

> 清单是**手写契约**而不是从代码生成的产物 —— 生成出来的清单永远等于代码，
> 就失去了"必须显式决定"的意义。测试只负责比对，改不改由人决定。
