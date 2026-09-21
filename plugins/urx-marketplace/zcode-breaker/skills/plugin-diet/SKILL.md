---
name: plugin-diet
description: 每轮工具 schema 税（token 成本）的控制面：现存 MCP 插件各自的每轮体积与**真实调用次数**、哪些建议常驻/按需、如何查询现状、如何开关插件（config.json + 重启）。当你做 token 节流、想知道"某个能力从哪来"、或需要临时启用某插件时用。
---

# 插件按需挂载（每轮 token 税的控制面）

## 为什么
ZCode 每轮请求固定携带**所有已启用 MCP 服务器**的工具 schema，与是否使用无关。
2026-09-22 实测：**299KB / 191 个工具 ≈ 87K token/轮**。会话越长这笔税付得越多（每轮重发）。
两个已知的误判，别踩：
- **skills / commands 是按需加载（免费）** —— 关掉不带 MCP 服务的插件**一个 token 都不省**
  （实测：300 个 skill 的目录不在请求体里）。所以"关插件"只关**带工具面的**那几个。
- **插件的 telemetry 不是调用记录**（那是它自己的内部账）——判"用没用过"要用
  **各会话响应的 `toolCalls`**（下面的 `plugin_cost.py` 已经这么算）。

## 现状清单（**2026-09-22 实测**；调用次数为全历史累计）

| 提供方 | 每轮体积 | 工具数 | 累计调用 | 处置 |
|---|---|---|---|---|
| ZCode 内置 | 150.9KB | 33 | 229 | **不可关**（Agent / Bash / Read / Workflow 等） |
| desktop-commander | 58.7KB | 26 | 8 | 🔴 **已关**（读写/进程与内置工具大量重复；收益最大） |
| unified-rx | 34.8KB | 80 | — | 常驻；首屏可收窄：`UNIFIED_RX_PROFILE=core`（80→54） |
| apollo-skills | 20.6KB | 14 | 0 | 🔴 **已关**（GraphQL/GraphOS） |
| playwright | 18.6KB | 25 | 0 | 🔴 **已关**（做 Web UI 自动化时再开） |
| context7 | 4.6KB | 2 | 0 | 保留（便宜且查库文档真有用） |
| microsoft-docs | 3.9KB | 3 | 0 | 🔴 **已关**（Azure/M365） |
| node_repl（browser-use 系） | 3.2KB | 1 | 0 | 按需（随浏览器任务） |
| mimosa | 2.6KB | 5 | 0 | **别关**——它同时提供本机钩子（session-guard / 安全扫描） |
| cloudflare-docs | 1.1KB | 2 | 0 | 🔴 **已关** |

**已落地（2026-09-22）**：上表 5 个 🔴 置 `false`；备份 `~/.zcode/cli/config.json.pre-diet-20260922`；
回滚 `python D:\...\workspace\default\plugin_diet.py --revert <备份>` 或把该行改回 `true`。
**预期**：工具面 299KB → ≈200KB（再加 unified-rx 收窄 ≈188KB ≈ 53K token/轮），省 ≈34K token/轮
⇒ 100 轮会话省 ≈3.4M token。**生效要重启**；重启后跑 `scripts/plugin_cost.py` 对账这一行。

## 怎么开关（要重启）
1. 编辑 `~/.zcode/cli/config.json` → `plugins.enabledPlugins`：把 `<插件名>@<市场>` 置
   `false`（或删掉该行）。例：`"apollo-skills@claude-plugins-official": false`
2. **重启 ZCode 生效**（插件在启动时挂载，会话中途不会动态出现/消失）。
3. 恢复同理（true / 加回）→ 重启。

## 怎么查现状（三招）
1. **每轮工具税 + 使用证据**：`python D:\开发\unified-rx-mcp\scripts\plugin_cost.py`
   （按提供方给体积/工具数/累计调用 + 「0 次 → 建议关」的可省总量；只读、不碰配置）。
2. **会话级花销**：`python <workspace>\token_audit.py`（零 LLM）——会话排行、放大倍数、
   固定成本占比、体积档；报告写 `~/.unified-rx/token-audit.md`，告警进 `alarms.jsonl`。
   另有 unified-rx 工具 `session_burn`（单看各会话 model-io 体积）。
3. **大输出别整段进上下文**：`bash <workspace>\cap.sh <名字> -- <命令…>`——输出落
   `~/.zcode/captures/`，只带首尾各 12 行；实测 35KB → 3KB。

## 给智能体的用法
- 做事前先想"这件事需要哪个插件"；若所需插件属于"按需"且当前被关 → 明确告诉用户
  开哪个、改哪一行、需重启，不要猜"没有这个能力"。
- "没有工具" ≠ "没有能力"：先查本表 + unified-rx 的 `capability_manifest`；unified-rx 侧
  非 core 域用 `profile_enable` 运行时开启。
- 会话卫生配套：长任务拆会话（每项目/每阶段新开或 /clear）；单会话 model-io 超
  15MB 就该收尾——session-guard 钩子会在 15/30/50/80MB 各提醒一次。
