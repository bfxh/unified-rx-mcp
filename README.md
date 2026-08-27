# unified-rx-v2

**本地工具代替智能体体力活的平台** — 凡是 AI 要做的确定性体力活，全部下沉为本地工具；AI 只保留决策层。
> 定位：**工具箱，不是智能体，不是内核**。MCP 只是通道，价值在"工具 + 工作流"的完整链路。
> 七维"掌握"：**结构 / 语义 / 定位 / 探索 / 记忆 / 反馈 / 质量**
> 设计哲学：**少而准**（36 个工具，不用 183 个噪音）· **零依赖可跑**（纯 stdlib）·
> **写文件通道必须可靠**（fs_write 带授权直传）· **单点接开源最强**（语义引擎/LSP 不自研）

## 与旧版 unified-rx-mcp 的关系

| | 旧 unified-rx-mcp | unified-rx-v2（本仓） |
|---|---|---|
| 工具面 | 183（注入面 200+） | **36 个组合工具 / 12 域** |
| server | 7462 行上帝文件 | 协议薄层 + tools/ 按域 |
| 依赖 | mcp SDK + 多扩展 | **纯 stdlib 零依赖** |
| 写文件 | 授权剥离（写不了） | `__authorized` 直传，可控 |
| 检索 | 5 套并行 | code_search 统一（可接 codegraph） |
| 代码智能 | 手写 AST 文本规则 | 结构化扫描层 + **真 LSP 客户端** |

## 工具面（12 域 · 36 工具 @ S18）

| 域 | 工具 | 说明 |
|---|---|---|
| 📁 fs | `fs_read` `fs_write` `fs_stat` `fs_list` | 沙盒 fail-closed；写授权直传 |
| 🐛 scan | `bug_scan` `std_check` `ui_check` `bug_locate` `project_scan` `ast_scan` | S9 结构化层（py 真 AST / JS 词法管线 / Rust fn 归属）；S16 跨文件可达性归档 `reach ∈ {prod, test_only, unreferenced}` |
| 🛠️ ide | `locate_edit` `code_context` `ide_edit_multi` `ide_rename` | 文本级定位与编辑引导 |
| 🧠 lsp | `ide_lsp` | **真 LSP 客户端（S17）**：rust-analyzer / pylsp——definition/references/hover/symbols/diagnostics/**rename 只出预案不落盘** |
| 🔍 search | `code_search` | BM25 符号加权（中英/标识符 → file:line） |
| 🛡️ guard | `hallucination_guard` `capability_manifest` | file:line/符号/工具名核查；H2 一致率实测 100% |
| 🧠 learn | `lesson` | 教训记忆（recall/add/feedback） |
| ⚙️ ops | `backup` `scan_log` `usage_stats` `project_health` `lesson_stats` | 打点自动写入 stats.jsonl；趋势并入 scan_log |
| 🎮 game | `game_check` `blender_verify` | 游戏规则检查 / Blender 截图验证 |
| 🚀 engine | `engine_status` `engine_query` | codegraph 优先、BM25 自动降级（带原因字段） |
| 🕵️ attack | `input_fuzz` `path_probe` `big_input` | 自攻面常驻（S7）：全 fuzz 集 100% 拒绝已固化为 pytest |
| 🧬 appaudit | `app_audit` `app_clone` `app_clean` | 智能体/桌面应用自查三件套（asar 自标定提取） |

已于 S15 移除的废物面（证据驱动）：kb_query / chatlog_search / cmd_cheatsheet /
code_complete / ide_references / cost_report / trend_analysis / pipeline / parallel / pure_*。

## 运行

```bash
python server.py            # MCP stdio 模式
python server.py --selftest # 协议自检（schema 门禁）
python -m pytest tests/ -q  # 全量测试（128 passed 基线）
```

LSP 能力需要宿主装有对应语言服务器：

```bash
rustup component add rust-analyzer            # Rust
python -m pip install python-lsp-server pycodestyle pyflakes   # Python 诊断需 lint 后端
```

缺失时 `ide_lsp` 的 status 如实报 detected=false 并降级到文本级 ide 工具——绝不假装支持。

## 评测体系（spec/EVAL.md 五假设 → 四项已实测）

| 假设 | 结论 | 关键数字 |
|---|---|---|
| H1 工具省轮次省 token / 提准确率 | ✅ 双通道复现 | Δsolved **+6.7pp**(deepseek-chat, n=90) / **+10pp**(glm-4.5-flash)；文件引用存在率 0% vs 63%/23% |
| H2 hallucination_guard 拦得住假声明 | ✅ 首测达标 | 与路径真值一致率 **100%**（1379 条声明，门槛 ≥90%） |
| H3 扫描器真查准率 | ✅ 首测 PASS | 案底 FP 复检 0 命中保持；precision≈1.0（三条 WEAK(n=1) 黄灯如实亮着） |
| H4 lesson 记忆复利 | 待跨会话积累 | — |
| H5 fail-closed 可托管性 | ✅ 固化 pytest | 安全模糊集 100% 拒绝 |

L3 双臂评测器：`bench/ab_run.py`（run/judge/score 三模式），
语料 30 条真实历史缺陷 × rubric（bench/l3_tasks.jsonl），72→360 run 已入库可复跑。

## 施工史

S1-S18 全程对账见 [spec/UPGRADE.md](spec/UPGRADE.md)：安全收口 → 协议三件套 →
攻击面默认化 → appaudit 域 → 结构化扫描（AST/词法管线/Rust 切片）→ L2/L3 评测体系 →
内存 -75% / 缓存 16× → 废物清理（46→35 工具）→ 真 LSP 域 → H2/H3 上场。
