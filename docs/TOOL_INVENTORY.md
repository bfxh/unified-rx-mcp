<!-- SPDX-FileCopyrightText: 2026 bfxh -->
<!-- SPDX-License-Identifier: MIT -->
# 工具梳理清单（unified-rx 全工具 + 技术栈）

> 目标：理清"有什么工具、干什么用、哪些被调用、哪些 0 调用"——
> 用户要求工具梳理清楚（含 IDE/Qoder/树搜索/算法/缓存/知识共享）。
> 数据来源：`server.py` 注册表（101 核心；2026-08-16 核心合并 97→73，2026-08-19
> 增至 101——文档部分数字保留快照）+ `~/.unified-rx/stats.json`（实测）。

## 一、核心工具（73 个，server.py 注册表；2026-08-16 核心合并）

**组合化（同域族→1 个，action 分发，能力零丢失）**：`mesh`（boolean/check/clip/optimize/splat/union）、
`telemetry`（query/snapshot/status）、`game`（api/check/feel/rules/verify）、
`lesson`（recall/feedback/learn/extract/rule_feedback）、`replay`、`causal`、`half_edge`、
`repo`、`agent`、`geom`、`voxel`、`scan` 各 2→1；`bug_bisect` 并入 `bug_locate`（action=bisect）。
旧工具名不再暴露；lesson_learn/game_rules 内层子动作经 `sub_action` 透传。

调用次数 = stats.json 累计实测（2026-08-16）；`0` = 从未被调过（见第四节补用计划）。

| 域 | 工具（调用次数） |
|---|---|
| **🛡️ 防幻觉/能力边界** | `hallucination_guard`（1152）、`capability_manifest`（96）、`tool_card`（591）、`cmd_cheatsheet`（26） |
| **🗂️ 仓库认知/索引** | `cb_index`（660）、`cb_status`（209）、`cb_scan`（12）、`locate_edit`（336）、`kb_query`（96）、`repo_graph`（0）、`repo_wiki`（13）、`semantic_search`（39）、`code_search`（4）、`explore_code`（689） |
| **🐛 漏洞扫描/定位** | `bug_scan`（12705）、`vuln_scan`（2178）、`bug_locate`（245）、`bug_locate_feedback`（147）、`bug_bisect`（0）、`causal_link`（13）、`causal_trace`（13）、`ui_check`（304）、`predict_impact`（39）、`speculate`（26）、`optimize_code`（26） |
| **📏 工程标准** | `std_check`（11173）、`ds_check`（244）、`ds_lookup`（49）、`quality_scan`（0） |
| **🏃 项目/全盘扫描** | `project_scan`（815）、`full_scan`（230）、`scan_log`（96）、`scan_trend`（61）、`watch_status`（0） |
| **🧪 测试增强/复现** | `cov_scan`（0）、`stress_scan`（0）、`replay_record`（0）、`replay_run`（0）、`sage_scan`（2）、`failure_analyze`（0）、`alarm_check`（15） |
| **📡 遥测** | `telemetry_status`（20）、`telemetry_query`（20）、`telemetry_snapshot`（5） |
| **🌐 弱网模拟** | `net_chaos`（0） |
| **🛠️ IDE 增强** | `ide_actions`（2276）、`ide_complete`（96）、`ide_fusion`（39）、`ide_quest`（1105）、`ide_references`（0）、`ide_rename`（0）、`code_complete`（253）、`code_embed`（26）、`local_intel`（0） |
| **🧠 教训/进化记忆** | `lesson_recall_lse`（48）、`lesson_feedback`（144）、`lesson_learn`（104）、`lesson_extract`（0）、`rule_feedback`（144）、`design_note`（52）、`skill_fetch`（161） |
| **🎮 游戏方向** | `game_api`（52）、`game_check`（143）、`game_feel`（39）、`game_rules`（52）、`game_verify`（26）、`runtime_state`（26） |
| **📐 几何引擎** | `mesh_check`（52）、`mesh_optimize`（13）、`mesh_splat`（13）、`mesh_union`（13）、`mesh_clip`（26）、`mesh_boolean`（32）、`half_edge`（26）、`half_edge_adjacency`（26）、`geometry_exchange`（26）、`voxelize`（13）、`voxel_surface`（13）、`geom_graph`（26）、`geom_example`（52） |
| **🤖 多智能体** | `agent_orchestrate`（0）、`agent_roles`（0） |
| **⚙️ 纯函数** | `math_ops`（52401）、`text_ops`（196）、`sort_search`（222）、`stat_geo`（235）、`json_email`（196）、`prime_list`（245）、`fib_fibonacci`（173） |
| **🔧 协作/调度** | `pipeline`（192）、`parallel`（2080）、`local_run`（26） |
| **📁 文件层** | `fs_read`（100）、`fs_write`（170）、`fs_stat`（0）、`fs_list`（0） |
| **🧩 补丁学习** | `patch_learn`（48） |

## 二、扩展工具（76 个，lazy-loaded，2026-08-16 全合并）

| 扩展 | 工具 | 用途 | 调用状态（stats 实测） |
|---|---|---|---|
| code-analysis-enhance | `cae_*`（13） | 文件去重/语言检测/LSP 查询/代码上下文/变更影响/教训召回 | cae_change_impact 158 次在用；lsp_query/code_context 仍 0（必须补用） |
| pr-oracle | `pr_oracle_map_pr`/`map_local`/`discover_tests` | PR→测试影响 | 0 调用（必须补用） |
| tautest | `tautest_run`/`doctor`/`init`/`demo` | 变异测试 | demo 用过；run/doctor 0（大改动后补用） |
| stats | `stats_record`/`summary`/`status`/`clear` | 调用统计打点 | 自动打点中（本表数据源） |

## 三、技术栈梳理（"各种技术各种算法一起来的"）

| 技术 | 实现位置 | 状态 |
|---|---|---|
| AST 静态分析 | server.py `_bug_*`（Python ast，23 语言） | 在用 |
| 树搜索（UCB） | lse-engine `ucb_select/backprop` | 在用（bug_locate） |
| 教训进化（LSE） | lse-engine（Rust） | 在用 |
| 语义检索（BM25+符号加权） | **rx-search**（Rust，零依赖） | 在用（code_search） |
| 弱网模拟（混沌代理） | **rx-net**（Rust，纯 std TCP 代理） | 在用（net_chaos） |
| 遥测（流式 tail/JSONL） | **rx-telemetry**（Rust） | 在用（telemetry_*） |
| 纯函数 Rust 层 | **rx-core**（R1 已接线：25 动作白名单 + 常驻子进程 + Python 回退） | **在用**（2026-08-16 首次编译生效，parity 2310 例 0 mismatch） |
| LSP 语义 | cae_lsp_query（pylsp/rust-analyzer/clangd） | 0 调用（能力已验证） |
| 素数筛/排序/统计/几何 | prime_list/sort_search/stat_geo | 在用 |
| 正则引擎 | std_core/guard_core | 在用 |
| 并发模型 | ThreadPoolExecutor 多路 | 在用 |
| 缓存 | scan_cache（mtime_ns 键 + LRU） | 在用 |
| 知识共享 | scan-log/lse-state/stats | 在用 |

## 四、0 调用工具补用计划（按优先级）

实测 0 调用核心工具 18 个：`agent_orchestrate`、`agent_roles`、`bug_bisect`、`cov_scan`、`failure_analyze`、`fs_list`、`fs_stat`、`ide_references`、`ide_rename`、`lesson_extract`、`local_intel`、`net_chaos`、`quality_scan`、`replay_record`、`replay_run`、`repo_graph`、`stress_scan`、`watch_status`

1. **cae_lsp_query / cae_code_context / cae_change_impact / cae_lesson_recall**：改代码前后语义验证——**2026-08-16 已实跑补用**（rx-core 接线/探针任务中全部调用成功）
2. **pr_oracle_map_pr / pr_oracle_map_local**：PR 合并前跑测试影响分析
3. **tautest_run / tautest_doctor**：大改动后跑变异测试
4. **bug_bisect / replay_* / cov_scan / stress_scan**：新族（阶段 3 测试增强）——遇到偶现 bug/回归场景时用
5. **net_chaos**：刚上线（阶段 5）——测网络鲁棒性场景时用（HTTP 客户端/下载/同步类任务）
6. **fs_stat / fs_list / watch_status / local_intel / lesson_extract / repo_graph 等**：低频能力，按需启用

## 五、扫描模式与工具映射

| 模式 | 工具 | 循环 |
|---|---|---|
| ① 全盘扫 | full_scan（排除 Steam/无关） | daemon-full |
| ② 项目扫 | project_scan（四路并行 bug/std/ui/cb） | daemon-project / rx-scan-project |
| ③ 只扫自己 | self_scan（全家并发） | daemon-self / rx-scan-self |
| ④ 被 RX 调用 | 任何扫描工具调用即记 | 天然 |
| ⑤ 仓库管理 | 7 仓库 PR/CI 轮询 → repo-log | daemon-repo |
| 缓存 | scan_cache（mtime_ns+LRU 512） | daemon-cache |
| 知识共享 | scan-log.jsonl（全模式统一落盘） | 全循环 |
