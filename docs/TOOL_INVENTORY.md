<!-- SPDX-FileCopyrightText: 2026 bfxh -->
<!-- SPDX-License-Identifier: MIT -->
# 工具梳理清单（unified-rx 全工具 + 技术栈）

> 目标：理清"有什么工具、干什么用、哪些被调用、哪些 0 调用"——
> 用户要求工具梳理清楚（含 IDE/Qoder/树搜索/算法/缓存/知识共享）。

## 一、核心工具（35 个，server.py 注册表）

| 分类 | 工具 | 用途 | 调用状态（stats 实测） |
|---|---|---|---|
| 文件层 | fs_read/fs_write/fs_stat/fs_list | 安全文件读写（沙盒+大小上限） | 高频（fs_read 影子触发源） |
| 数学 | math_ops/fib_fibonacci | 加减乘除/幂/阶乘/斐波那契 | 最高频（43608 次） |
| 文本 | text_ops | reverse/upper/lower/palindrome | 高频 |
| 排序搜索 | sort_search | quick/bubble/binary | 高频 |
| 统计几何 | stat_geo | mean/median/circle/rect | 高频 |
| JSON/校验 | json_email | parse/valid/email | 高频 |
| 素数列表 | prime_list | is_prime/generate/unique/flatten | 高频 |
| 漏洞扫描 | bug_scan/vuln_scan | AST 静态 bug + 三路并行 | 高频（1383 次） |
| 定位 | bug_locate/bug_locate_feedback | traceback→file:line + UCB 反馈 | 高频 |
| **Qoder 式定位** | locate_edit | 自然语言→修改位置 | 在用（99 次） |
| **IDE/LSP** | code_complete | LSP 补全（5 语言） | 在用（168 次） |
| 项目扫描 | project_scan/full_scan | 四路并行/多项目并发 | 在用 |
| 工程标准 | std_check | 占位/命名/硬编码/魔法数字 | 在用 |
| 防幻觉 | hallucination_guard | 声明三分级验证+回灌 | 高频（465 次） |
| 能力清单 | capability_manifest | 有什么/没有什么 | 在用 |
| 扫描日志 | scan_log | 专项目查扫描历史 | 在用 |
| 协作 | pipeline/parallel/tool_card | 配方/并发/结构化回喂 | 在用 |
| 代码库认知 | cb_index/cb_status/cb_scan | 索引/变更感知/扫描 | 在用 |
| 设计系统 | ds_lookup/ds_check | token 查询/合规 | 在用 |
| 教训 | lesson_recall_lse/lesson_feedback/rule_feedback | LSE 教训召回/反馈 | 在用 |
| **树搜索** | bug_locate_feedback（UCB 树） | bug 定位候选 UCB 选择+回流 | 在用（102 次） |

## 二、扩展工具（24 个）

| 扩展 | 工具 | 用途 | 调用状态 |
|---|---|---|---|
| code-analysis-enhance | cae_file_dedup_state/cae_aether_lang_support | 文件去重/语言检测 | 在用（33 次） |
| code-analysis-enhance | **cae_lsp_query/cae_code_context/cae_change_impact/cae_lesson_recall** | **IDE 核心语义工具** | **0 调用（必须补用）** |
| code-analysis-enhance | cae_aether_agent_parse/goto_parse/model_provider/probe | Aether 协议 | probe 用过 1 次 |
| code-analysis-enhance | cae_lsp_position_convert/semantic_tokens_decode/edit_merge | LSP 算法 | 0 调用 |
| pr-oracle | pr_oracle_map_pr/map_local/discover_tests | PR→测试影响 | **0 调用（必须补用）** |
| tautest | tautest_run/doctor/init | 变异测试 | 0 调用（demo 33 次） |
| stats | stats_record/summary/status/clear | 调用统计 | 0 调用（自动打点中） |

## 三、技术栈梳理（"各种技术各种算法一起来的"）

| 技术 | 实现位置 | 状态 |
|---|---|---|
| AST 静态分析 | server.py `_bug_*`（Python ast） | 在用 |
| 树搜索（UCB） | lse-engine `ucb_select/backprop` | 在用（bug_locate） |
| 教训进化（LSE） | lse-engine（Rust） | 在用 |
| LSP 语义 | cae_lsp_query（pylsp/rust-analyzer/clangd） | 0 调用（能力已验证） |
| 素数筛 | prime_generate（埃氏筛） | 在用 |
| 二分/快排/冒泡 | sort_search | 在用 |
| 统计/几何 | stat_geo | 在用 |
| 正则引擎 | std_core/guard_core | 在用 |
| **Rust 纯函数层** | rx-core（迁移一期，PR #8） | 待合并 |
| 并发模型 | ThreadPoolExecutor 多路 | 在用 |
| 缓存 | scan_cache（mtime_ns 键 + LRU） | 新（本期） |
| 知识共享 | scan-log/lse-state/stats | 在用 |

## 四、0 调用工具补用计划（按优先级）

1. **cae_lsp_query**：改代码前跳转/悬停/引用——REASONIX.md 已立规则，收尾抽查执行
2. **cae_code_context**：写代码前取符号级上下文——同上
3. **cae_change_impact**：改完代码跑变更影响——同上
4. **pr_oracle_map_pr**：PR 合并前跑测试影响分析
5. **tautest_run**：大改动后跑变异测试
6. **cae_lsp_position_convert**：LSP 算法工具（byte↔position）

## 五、扫描模式与工具映射（本期架构）

| 模式 | 工具 | 循环 |
|---|---|---|
| ① 全盘扫 | full_scan（排除 Steam/无关） | daemon-full |
| ② 按窗口扫 | window_core→project_scan | daemon-window |
| ③ 只扫自己 | self_scan（全家并发） | daemon-self |
| ④ 被 RX 调用 | 任何扫描工具调用即记 | 天然 |
| ⑤ 影子扫描 | shadow_core→bug_scan/std_check | daemon-shadow |
| 缓存 | scan_cache（mtime_ns+LRU 512） | daemon-cache |
| 知识共享 | scan-log.jsonl（全模式统一落盘） | 全循环 |
