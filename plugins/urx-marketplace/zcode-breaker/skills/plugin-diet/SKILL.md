---
name: plugin-diet
description: 每轮工具 schema 税（token 成本）的控制面：现存 MCP 插件各自的每轮体积、哪些建议常驻/按需、如何查询现状、如何开关插件（config.json + 重启）。当你做 token 节流、想知道"某个能力从哪来"、或需要临时启用某插件时用。
---

# 插件按需挂载（每轮 token 税的控制面）

## 为什么
ZCode 每轮请求固定携带**所有已启用 MCP 服务器**的工具 schema，与是否使用无关。
2026-09-14 实测：**205KB / 170 个工具 ≈ 5–6 万 token/轮**。会话越长这笔税付得越多
（每轮重发）。注意：插件里的 skills / commands 是**按需加载（免费）**，只有带 MCP
服务器的插件才收税——所以"关插件"要关的是列表里这些带工具面的。

## 现状清单（2026-09-15 实测，按每轮体积排序）

| 提供方 | 每轮体积 | 工具数 | 建议 |
|---|---|---|---|
| desktop-commander | 58.7KB | 26 | 日常文件/进程操作用得多 → 常驻；不常用则关（收益最大） |
| ZCode 内置 | 57.3KB | 21 | 不可关（含 Agent / AskUserQuestion 等） |
| computer-use（GUI 控制） | 56.9KB | 30 | 只在需要操作 GUI 时开——按需项里收益最大 |
| unified-rx | 32.6KB | 72 | 常驻（本地工具箱） |
| apollo-skills | 19.8KB | 14 | 不常做 GraphQL/GraphOS → 关 |
| playwright | 17.1KB | 24 | 只在浏览器自动化时开 |
| context7 | 4.6KB | 2 | 只在查第三方库文档时开 |
| microsoft-docs | 3.9KB | 3 | 只在微软/Azure 任务时开 |
| node_repl（browser-use 系） | 2.8KB | 3 | 只在浏览器任务时开 |
| mimosa | 2.6KB | 5 | 只在安全扫描时开 |
| cloudflare | 1.1KB | 2 | 只在 Cloudflare 任务时开 |

合计 **257KB / 202 个工具 ≈ 7 万 token/轮**（会话内每轮都付）。把"按需"项关掉
（computer-use + apollo + playwright + context7 + microsoft-docs + node + mimosa +
cloudflare）≈ **省 105KB/轮（≈3 万 token/轮）**；按 300 轮/日 ≈ 900 万 token/日。

## 怎么开关（要重启）
1. 编辑 `~/.zcode/cli/config.json` → `plugins.enabledPlugins`：把 `<插件名>@<市场>` 置
   `false`（或删掉该行）。例：`"apollo-skills@claude-plugins-official": false`
2. **重启 ZCode 生效**（插件在启动时挂载，会话中途不会动态出现/消失）。
3. 恢复同理（true / 加回）→ 重启。

## 怎么查现状（两招）
1. `session_burn`（unified-rx 工具）：各会话 model-io 体积（≈累计 prompt 字节，
   MB×~28万≈token 量级）与当档阈值；越档会写 `~/.unified-rx/alarms.jsonl`。
2. 刷新"每轮工具税"表（解析最近一次模型请求的工具面）：
   `python D:\开发\unified-rx-mcp\scripts\plugin_cost.py`（若不存在，临时用：
   读 `~/.zcode/cli/rollout/model-io-*.jsonl` 最新文件末行 → `request.body.tools`
   → 按 `mcp__plugin_<插件>_` 前缀分组统计字节数）。

## 给智能体的用法
- 做事前先想"这件事需要哪个插件"；若所需插件属于"按需"且当前被关 → 明确告诉用户
  开哪个、改哪一行、需重启，不要猜"没有这个能力"。
- "没有工具" ≠ "没有能力"：先查本表 + unified-rx 的 `capability_manifest`。
- 会话卫生配套：长任务拆会话（每项目/每阶段新开或 /clear）；单会话 model-io 超
  15MB 就该收尾——session-guard 钩子会在 15/30/50/80MB 各提醒一次。
