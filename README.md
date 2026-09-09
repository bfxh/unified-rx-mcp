# unified-rx-v2

**本地工具代替智能体体力活的平台** — 凡是 AI 要做的确定性体力活，全部下沉为本地工具；AI 只保留决策层。
> 定位：**工具箱，不是智能体，不是内核**。MCP 只是通道，价值在"工具 + 工作流"的完整链路。
> 七维"掌握"：**结构 / 语义 / 定位 / 探索 / 记忆 / 反馈 / 质量**
> 设计哲学：**少而准**（64 个工具，不用 183 个噪音）· **零依赖可跑**（纯 stdlib）·
> **写文件通道必须可靠**（fs_write 带授权直传）· **单点接开源最强**（语义引擎/LSP 不自研）

**当前 v2.39.0（S121）**：64 工具 / 12 域；pytest 3.14 = 728 passed + 2 skipped、
3.11 = 730 passed；cargo test 156 绿；selftest 机器对账三行全绿（VERSION_TAG /
SKILLS_DOCS / EXE_TAG）。现状坐标见 [spec/PANORAMA.md](spec/PANORAMA.md)，
逐轮决策与证据见 [spec/ROUNDLOG.md](spec/ROUNDLOG.md)。

## 与旧版 unified-rx-mcp 的关系

| | 旧 unified-rx-mcp | unified-rx-v2（本仓） |
|---|---|---|
| 工具面 | 183（注入面 200+） | **64 个组合工具 / 12 域** |
| server | 7462 行上帝文件 | 协议薄层 + tools/ 按域 |
| 依赖 | mcp SDK + 多扩展 | **纯 stdlib 零依赖**（Rust 侧 `[dependencies]` 恒空） |
| 写文件 | 授权剥离（写不了） | `__authorized` 直传，可控 |
| 检索 | 5 套并行 | code_search 统一（可接 codegraph） |
| 代码智能 | 手写 AST 文本规则 | 结构化扫描层 + **真 LSP 客户端** + 19 件 ide 工具 |

## 工具面（12 域 · 64 工具）

| 域 | 工具 |
|---|---|
| 📁 fs (4) | `fs_read` `fs_write` `fs_stat` `fs_list` — 沙盒 fail-closed；读面纯 Python（S95 回迁，golden oracle 锁等价），写面 rx-fs.exe |
| 🐛 scan (14) | `bug_scan` `std_check` `ui_check` `bug_locate` `project_scan` `ast_scan` `code_review` `dep_graph` `module_stability` `code_coverage` `vuln_knowledge` `ast_grep` `file_scan` `near_dupes` — 正则 + AST-lite，非编译器语义；覆盖矩阵见 VULN-HUNTING 附录 B |
| 🛠️ ide (20) | `ide_outline` `ide_read_symbol` `locate_edit` `code_context` `ide_edit_multi` `ide_batch_edit` `ide_rename` `ide_lsp` `ide_impact` `ide_diagnostics` `ide_build` `ide_test` `ide_debug` `ide_break` `ide_doctor` `ide_multi_check` `ide_vscode` `ide_auto_report` `ide_health_trend` `scip_refs` |
| 🔍 search (3) | `code_search`（BM25，`hybrid=true` 时与语义路 RRF 融合）`code_semantic`（tf-idf 定义级）`repo_map`（个人化 PageRank 符号地图） |
| 🛡️ guard (2) | `hallucination_guard` `capability_manifest` — 声明核查；读取过沙盒（S97） |
| 🧠 learn (1) | `lesson` — 教训记忆（关键词检索，非向量） |
| ⚙️ ops (5) | `backup` `scan_log` `usage_stats` `project_health` `lesson_stats` |
| 🎮 game (2) | `game_check` `blender_verify` |
| 🚀 engine (2) | `engine_status` `engine_query` |
| 🕵️ attack (5) | `input_fuzz` `path_probe` `big_input` `rust_taint_scan` `auth_gate_sweep` — 自攻面常驻 |
| 🧬 appaudit (3) | `app_audit` `app_clone` `app_clean` |
| 🧰 meta (3) | `local_run` `process` `gpu_status` — 授权门控 / GPU 遥测 |

已于 S15 移除的废物面（证据驱动）：kb_query / chatlog_search / cmd_cheatsheet /
code_complete / ide_references / cost_report / trend_analysis / pipeline / parallel / pure_*。

## 运行

```bash
python server.py            # MCP stdio 模式
python server.py --selftest # 协议自检 + 机器对账（schema/版本/skills/exe）
python -m pytest tests/ -q  # 全量测试（当前 602 passed + 2 skipped 基线）
```

LSP 能力需要宿主装有对应语言服务器：

```bash
rustup component add rust-analyzer                            # Rust
python -m pip install python-lsp-server pycodestyle pyflakes   # Python
```

缺失时 `ide_lsp` 的 status 如实报 `detected=false` + reason（S99 修：`python -m`
形态必须验到模块层，不再因 exe=解释器而假阳性），`ide_impact` 自动降级为文本级
影响面（`engine="text"` + 精度标注）——绝不假装支持。

## Rust 原生化（S78 起）

16 件薄壳化：`fs_write`、search 双件、scan 五件、appaudit 三件、ide 五件；
其余结构性留 Python（判型在案，见 VULN-HUNTING）。fs 读面 S95 回迁纯 Python
（微秒级操作不付进程拉起溢价：fs_stat p50 7.7-9.7ms → 0.3ms）。
红线：`[dependencies]` 恒空；每轮 pytest + cargo test 双绿才准合入；
exe 缺失报清晰错误、不静默降级；沙盒语义两侧等价（WSL 面实测）。

## 评测体系（详见 spec/EVAL.md；H1-H4 台账在 §8）

| 假设 | 结论 | 关键数字 |
|---|---|---|
| H1 任务增益 | ✅ 双臂实测 | Δsolved **+6.7pp**（deepseek-chat n=90/90）、**+10pp**（glm-4.5-flash n=90/50）；**口径校正**：收益是"解决率 + 可核验性"（裸模型文件引用存在率 0% vs 工具组 63%/23%），不是"省 token"——轮次/成本反而升（§8） |
| H2 幻觉守卫 | ✅ 首测达标 | A 臂一致率 **1.0**（652 条）、B 臂 **0.9295**（766 条）；漏判（不存在却放行）**0**；分歧全为行级严判 |
| H3 扫描器查准 | ✅ 复测 PASS | precision≈1.0（api_key_sk 6/6；三条 WEAK(n=1) 黄灯如实亮着）；案底 FP 复检 0 命中 |
| H4 记忆复利 | ✅ 缩影 | 8 任务带教训复跑 solved 0/8→3/8、fail 点 -72%（复跑需 API 预算，挂账⑥） |
| H5 fail-closed | ✅ 固化 pytest | 安全模糊集 100% 拒绝 |

三阶段评测管线（bench/）：**L3** 双臂 A/B（ab_run.py，330 份答案入库）→
**P3** SWE-bench 外锚（swe_p3/swe_verify/swe_repair 三段）→ **P1** 标注 bug 库
（p1_build/p1_score）。延迟/内存基线见 §6（S94 立账）/ §7（S95 复测）。

## 施工史

S1-S26 全程对账见 [spec/UPGRADE.md](spec/UPGRADE.md)；S27 起逐轮见
[spec/ROUNDLOG.md](spec/ROUNDLOG.md)。近期弧线：Rust 原生化逐域推进（S78-S97）→
质量门禁机器化（S91-S94：selftest 三行对账 + 版本锁步）→ fs 读面回迁 + 高压电池
（S95）→ 副本深扫分诊（S96）→ 读面沙盒补漏（S97）→ 规则覆盖矩阵（S98）→
ide 检测/降级两修 + 文档刷新（S99）。
