<!-- SPDX-FileCopyrightText: 2026 bfxh -->
<!-- SPDX-License-Identifier: MIT -->
# MCP 接口架构（MCP Interface Architecture）

> 用户概念（2026-08-12 定稿）：
> "不叫总线了，叫 MCP 接口。MCP 接口有一个小总，一般都会被影响。
> 引导差不多是 IDE 的功能，还有动态索引，没有引导线。
> 适配 RX，因为是 MCP。"

## 核心：MCP 接口 = 被调用状态

**MCP 接口不是新的程序，而是一个"状态"**：unified-rx 被 RX 调用时的入口。
接口内有一个**小总**（内部汇聚点）——所有被调用都过小总，被调用时产生各种耦合。
**接口不影响工具自身启动的各种东西**（daemon 自启动扫描照常跑）。

```
                ┌─────────────────────────────────────────┐
   RX 调用 ────▶│  MCP 接口（被调用状态）                    │
                │  ┌───────────────────────────────────┐  │
                │  │  小总（内部汇聚点）                   │  │
                │  │  ① 分发到具体工具                    │  │
                │  │  ② 自动落盘 scan-log                │  │
                │  │  ③ 触发耦合（影子扫描/索引关联/缓存）   │  │
                │  │  ④ 返回附已知问题（日志闯进调用）       │  │
                │  └───────────────────────────────────┘  │
                └──────────┬─────────────┬───────────────┘
                           │             │
              ┌────────────▼───┐  ┌──────▼──────────────┐
              │ IDE 式引导      │  │ 动态索引             │
              │ （类比 IDE 功能）│  │ cb_index/cb_status  │
              │ 补全/跳转/悬停/  │  │ /cb_scan           │
              │ 提示/验证       │  │ （被调用自动关联）    │
              └────────────────┘  └─────────────────────┘
```

## 一、小总（内部汇聚点，被调用都过这里）

小总 = 现有 `_call` 分发（server.py）。每次工具被调用，小总依次做 4 件事：

| 步骤 | 实现 | 说明 |
|---|---|---|
| ① 分发 | `_call` → `_TOOLS[name]` | O(1) 静态注册表分发到具体工具 |
| ② 落盘 | `_scan_log_tick` | 扫描类工具自动追加 scan-log.jsonl（知识共享数据源） |
| ③ 耦合 | 影子扫描/索引关联/缓存 | 被调用文件→影子跟随扫；cb_* 变更感知；bug_scan 缓存命中 |
| ④ 反馈 | `_attach_known_issues` | 返回附该路径最近已知问题（日志闯进调用） |

小总被"一般都会被影响"：任何被调用都会经过它，产生日志/耦合/反馈——这是
接口与自启动扫描的**唯一交叉点**（自启动扫描也调 `_call`，同样过小总落盘）。

## 二、IDE 式引导（没有"引导线"）

被调用的一部分工具是**引导性质**——类比 IDE 的功能（补全/跳转/悬停/提示/验证），
引导 AI 怎么干活：

| IDE 类比 | MCP 工具 | 用途 |
|---|---|---|
| 能力面板 | capability_manifest | 有什么/没有什么（防能力幻觉） |
| 代码补全 | code_complete / cae_lsp_query(completion) | LSP 补全（5 语言） |
| 跳转定义 | cae_lsp_query(definition/references) | 符号跳转/引用 |
| 悬停提示 | cae_lsp_query(hover) | 类型/文档提示 |
| 上下文感知 | cae_code_context / locate_edit | 光标符号上下文 / Qoder 式定位 |
| 类型检查提示 | cae_change_impact | 改完代码的变更影响 |
| 事实核查 | hallucination_guard | 引用前验证（防幻觉） |
| 历史教训 | lesson_recall_lse | 防复发（LSE 进化记忆） |
| 扫描历史 | scan_log | 专项目看日志 |

**没有"引导线"这个叫法**——就是 IDE 式引导能力，与被调用强关联。

## 三、动态索引（被调用自动关联）

`cb_index` / `cb_status` / `cb_scan`：AI 分析仓库时被调用，索引**实时反映代码库**
（变更感知：changed/added/removed），结果供后续调用复用（知识共享）。
索引状态持久化 `.unified-rx-index/index.json`，跨调用/跨对话共享。

## 四、日志闯进调用（核心反馈链路）

**扫描日志直接反馈进调用结果**：AI 调用扫描类工具（bug_scan/std_check/locate_edit/
vuln_scan/project_scan/ui_check）时，返回 JSON 自动附带 `known_issues`：

```json
{
  "ok": true,
  "issue_count": 0,
  "issues": [],
  "known_issues": [
    {"tool": "bug_scan", "ts": "2026-08-12 08:51:25", "summary": "issues=0 ok=True"}
  ],
  "known_issues_note": "来自 scan-log（日志闯进调用）：该路径最近的已知问题，修复进展可查 scan_log"
}
```

智能体**不用额外查日志**就知道"这文件出过什么 bug、修复到什么程度"（含后续维护）。
实现：`_attach_known_issues`（每次扫描调用后从 scan-log 回读该 root 最近 3 条）。

## 五、与自启动扫描的边界

**MCP 接口只描述"被调用时发生什么"；自启动扫描独立于接口运行**：

```
unified-rx-daemon（计划任务 AtLogOn 自启，工具活着就跑）
├── daemon-self    自扫全家（core+scripts+lse-engine+vendor）
├── daemon-project 活跃项目（UNIFIED_RX_PROJECT / stats 最活跃）
├── daemon-full    全盘扫（排除 Steam/无关目录）
├── daemon-repo    仓库管理（7 仓库 PR 轮询）
├── daemon-shadow  影子扫描（RX 调用哪个文件→跟着扫）
├── daemon-window  按窗口扫（活动窗口项目）
└── daemon-cache   缓存维护
```

这些不经过 MCP 接口——工具活着就跑；接口（小总）只是被调用时的汇聚与反馈。
唯一的交叉：自启动扫描调 `_call` 时也过小总（落盘/耦合一致）。

## 六、适配 RX（因为是 MCP）

| 适配项 | 配置 | 状态 |
|---|---|---|
| 自动启动 | `.mcp.json` auto_start:true + tier | 已配 |
| v2 manifest | `reasonix-plugin.json`（apiVersion reasonix.io/plugin/v2） | 备用 |
| config.toml 注册 | `[[plugins]] unified-rx` auto_start=true | 已注册 |
| 协议 | stdio newline-delimited JSON（mcp python SDK ≥1.9） | 已适配 |
| 事件循环 | list_tools/call_tool async handler，同步路径禁 asyncio.run | 已修复 |
| 沙盒 | UNIFIED_RX_SANDBOX + D:\开发 自动并入（仅沙盒启用时） | 已实现 |
| 默认调用规则 | REASONIX.md（IDE 引导/动态索引/小总反馈） | 已立 |

## 七、技术挂载（用户给的技术都在这框架上补上去）

| 技术 | 挂载点 | 链路 |
|---|---|---|
| IDE/LSP | cae_lsp_query / code_context / change_impact | 被调用→结果→日志→反馈 |
| Qoder 定位 | locate_edit | 同上 |
| 树搜索（UCB） | bug_locate_feedback（lse-engine） | 同上 |
| 算法（Rust） | rx-core（迁移一期） | 纯函数层 |
| 缓存 | scan_cache | 被调用命中/失效 |
| 知识共享 | scan-log / lse-state / stats | 全链路落盘 |
