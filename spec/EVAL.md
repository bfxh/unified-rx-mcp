# unified-rx-mcp 价值与评测设计（EVAL）

> 回答一个问题：**这个工具箱对真实项目 / 对 API 大模型到底有什么用，怎么证明。**
> 原则：工具箱不抢智能体的活（决策/写码/判断）；只做"规则可枚举的确定性体力活"。
> 一切结论用证据说话；不可核实的基准不进硬门槛。

---

## 1. 价值假设（可证伪）

| # | 假设 | 面向 | 证明指标 | 当前证据 |
|---|---|---|---|---|
| H1 | 探索下沉为本地调用 → API 模型省轮次省 token | API 成本/质量 | 同任务 A/B：解决率 Δ、平均轮次、input tokens、$、墙钟 | ✅ S14 双通道交叉复现：Δsolved +6.7pp(deepseek-chat, n=90/90) 与 +10pp(glm-4.5-flash)；可核验性决定性差异稳定（裸模型文件引用存在率 0%，工具组 63%/23%） |
| H2 | hallucination_guard 拦得住假声明 | 质量 | guard 判 verified/refuted 与真实 file:line 的一致率 ≥90% | 结构在，未测一致率 |
| H3 | bug_scan/std_check/ui_check 在扫描器层面有真查准率 | 真实项目 | 标注库上 precision ≥70%（1 FP ≤2 TP） | ✅ S18 首测：案底 FP 复检 0 命中（修复保持）、panic 家族 VF3 现场覆盖 ✓；4 规则 precision≈1.0 但三条 WEAK(n=1)——样本量不足如实亮黄灯（bench/h3_score.py） |
| H4 | lesson 记忆复利：同型任务重复犯错率下降 | 长期 | 召回后同类错误复发计数下降 | ✅ S20 缩影首测：B 臂 8 个全 fail 任务注入教训复跑——solved 0/8→3/8，fail 点 18→5（-72%），n 小作方向性证据 |
| H5 | fail-closed 沙盒 + 写授权 = 可托管性 | 安全 | 安全模糊集 100% 拒绝 | ✅ 本轮已固化为 pytest |

**一句话对外定位**：给 API 智能体的"本地证据层"——模型出决策，工具出事实；
省的是探索 token 和幻觉返工，赚的是确定性和可审计。

## 2. 三层评测体系

### L1 工具契约层（已有，硬门槛）
- `server.py --selftest` + `pytest tests/`（47 例）全绿。
- 任何改动不许倒退，这条是门不是目标。

### L2 工具质量层（新增，"不只是通过而是好"）
| 卡尺 | 标准 | 测法 |
|---|---|---|
| 安全边界 | 模糊集 100% 拒绝：env 未设/空串/空白串/symlink·junction 逃逸/伪造授权/超长路径/非字符串路径 | `tests/test_security_fuzz.py`（本轮落地） |
| 扫描器 P/R | bug_scan 在标注库（≥30 条自家历史真 bug + 干净样本）precision≥0.7 recall≥0.5 | L2 语料 + 打分脚本 |
| 延迟预算 | fs_* <10ms；scan 全仓 ≤2s/100 文件；engine_query ≤15s（含 BM25 降级） | 计时断言 → `bench/s94_perf.py` 实测留档（S94 首测见 §6：fs_* 贴地板，其余余量≥12倍） |
| 输出信噪 | 单结果默认 ≤200 行/≤50KB 不淹上下文；超限必须截断+摘要 | schema 抽检 |
| 失败语义 | 失败永远 `{ok:false,error}`，绝不抛穿协议层、绝不静默假成功 | 已覆盖 + 补录 |

### L3 任务增益层（核心创新点：replay-A/B）
unified-rx 是工具箱，不能直接跑 SWE-bench——要测的是**它给模型带来的增益**：

```
语料:   自家真实历史任务 30~50 个（VoxelForge 系 git log 的 bug 修复/小功能，
        每条 = 需求描述 + 复现步骤 + 验收标准 + gold patch）
双臂:   A=裸模型(API)   B=模型+39工具(同一API同参数)   各跑 n=3 取均值
指标:   解决率Δ(主)、平均轮次、input/output tokens、成本$、墙钟、上下文溢出次数
判分:   Agent-as-a-Judge 法（已核实，arXiv，Zhuge et al.;配套 DevAI 55任务/365层级需求）
        —— judge 按验收 rubric 逐条判 pass/fail；随机抽 10% 人工复核校准一致性
```

外锚（可选重仓，做完内测再上）：SWE-bench Verified 抽 20 题、GitTaskBench（AAAI，54 真实任务，
已核实 arXiv: Ziyi Ni et al.）选与文件/仓库操作相关的子集。

### 你贴的 benchmark 清单核实结论（2026-08-27, arXiv API）
| 名单项 | 结论 | 处置 |
|---|---|---|
| Agent-as-a-Judge / DevAI | ✅ 实证存在（Zhuge et al.） | 采用其判分方法论 |
| GitTaskBench | ✅ 实证存在（Ziyi Ni et al., 54 真实任务） | 可作外锚子集 |
| SWE-bench 家族 | ✅ 行业公认 | 外锚首选 |
| SWE-Compass / NL2Repo-Bench / FormulaCode / FeatureBench / FeatBench / PRDBench / "Claude Opus 4.7" | ❌ arXiv 查无实证（疑似转述失真） | **不进任何硬门槛**；哪天真找到了再评估 |

教训（对齐本项目 hallucination_guard 的精神）：二手 benchmark 清单本身就该过一遍 guard。

## 3. 设计质量评分卡（新增工具/add 功能必过，总分 90 制）

| 维度 | 满分 | 一句话问自己 |
|---|---|---|
| 确定性 | 10 | 同输入必同输出？无随机无巧合依赖？ |
| 安全边界 | 15 | 走沙盒了吗？写/执行要授权吗？fail-closed 吗？ |
| 失败语义 | 10 | 错误结构化返回，永不抛穿协议层？ |
| 输出信噪 | 10 | 结果是"证据"还是"噪音海"？会淹掉智能体上下文吗？ |
| 延迟预算 | 10 | 慢工具有没有后台化/超时/降级？ |
| 契约清晰度 | 10 | schema 描述一句话说清何时用、何时不用？ |
| 可测性 | 10 | 至少 1 例回归测试进 pytest？边界都测了？ |
| 最小面 | 10 | 替代了旧工具或归入既有域？（少而准） |
| 边界恪守 | 5 | 有没有偷偷替模型做判断？输出里有没有夹带解法代码？ |

≥75 才收录。`capability_manifest` 的 cannot 清单随每次改动同步。

## 4. 不抢活边界（测试固化，防工具越权进化）

1. 工具输出只含**事实与证据**（命中位置/统计/diff 建议），不含完整解题实现代码
2. `ide_rename` 类工具只建议不落盘；一切落盘必须走 `__authorized`
3. capability_manifest 必须维护"有什么/没有什么"，无法回答时返回 unverifiable
4. 每个 schema 描述里写明**何时不用**本工具（防智能体滥用替代自身推理）

## 5. 落地顺序

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 安全模糊集入库 pytest；本评分卡/EVAL 定稿 | ✅ 本轮完成 |
| P1 | 从 VoxelForge 系 git log 固化 30 条标注 bug 库（含干净样本）；bug_scan P/R 首测 | ✅ S26+S27 完成：30 条自标注语料（S26）+ **32 快照独立人工标注**（S27：评审者逐行语义判 safe/unsafe，独立于 scan 输出）。泛化测量：definite 家族零 FP；clue 召回审计揪出 indexing 正则漏 `[x.f as usize]` 缺口→已修（召回 1/3→3/3，真快照回归锁死）；clue 全量上报为设计使然不计 FP |
| P2 | replay-A/B runner（复用 server.py 的 stdio 协议 + `_mcp_probe` 思路），出第一份增益报告 | ✅ S14 完成：ab_run.py 实跑 12×3 双臂 72 run 全判，报告见 UPGRADE.md S14 / bench/results/l3/summary.json |
| P3 | 外锚：SWE-bench Verified 抽样 / GitTaskBench 子集 | ✅ S25 闭环：执行失败回喂修复轮（≤3 轮），verified A 1→3 / B 2→3（29 feasible）；判官 vs 执行 13/15 一致且分歧双向纠错（S24） |

## 附：A/B 判分 rubric 模板

```
task_id:      VF-xxx（来源 commit/issue）
需求清单:     R1..Rn（从 gold patch 反推的可验证行为点）
judge 流程:   对照 diff+运行结果逐条 R→pass/fail/unverifiable
主判定:       solved = 全部 Ri pass 且无额外破坏（回归测试绿）
抽检:         每 10 条随机 1 条人工复核；不一致则修 rubric 再批量
记录:         turns/tokens_in/tokens_out/cost$/walltime/工具调用序列
```

## 6. S94 质量体检基线（2026-09-08，v2.20.0，bench/s94_perf.py 实测）

回答「内存/性能/架构必须在标准上」：先有数，才谈达标。语料=临时目录 100 个 .py；
每项预热 3 轮后计时；冷跑=启动后首跑（exe/文件缓存全冷），热态=复跑。

**延迟实测（ms，min/p50/max）**：
| 工具 | 冷跑 | 热态 | L2 预算 | 判定 |
|---|---|---|---|---|
| fs_stat | 11.3/24.2/75.2 | 7.0/7.7-9.7/13.9 | <10ms | **贴地板**：热态过、冷跑闪红，无余量 |
| ast_scan(100f) | 107/116/164 | 49/50-55/65 | ≤2s | PASS，余量≥30倍 |
| engine_query | 85/93/119 | 33/34-40/52 | ≤15s | PASS，余量~300倍 |
| code_search | 73/87/130 | 33/35-43/73 | （无预算行） | 台账复测：S77 期 140ms 量级保持 |
| code_semantic | 1246/1423/1767 | 591/610-725/767 | （无预算行） | 观察值：语义索引每次调用重建，冷热敏感 |

**内存基线 + 泄漏 soak**：工作集基线 27.8MB（gc.collect 后口径，psapi
WorkingSetSize），15 轮混合负载（fs×10 + code_search + ast_scan）soak 后
27.9MB，Δ=+0.1MB，峰值 27.9MB → **无明显泄漏信号**。留档 bench/results/s94_perf.json
（滚动 20 条历史）。

**架构健康**（无上帝对象复核）：tools/*.py 最大 lsp.py 705 行；
rust/src 最大 pyast.rs 2989（解析器，合理单体）、astscan.rs 1784、taint.rs 1372。

**exe 版本对账**（S94 防呆四件）：9 bin `--version` 门（CARGO_PKG_VERSION 编译期
注入）→ selftest `EXE_TAG ok=9 drift=0 missing=0`（SKIP=纯 Python 环境不算漂移）
→ cargo `bin_version_test.rs` → pytest 版本锁步（SERVER_VERSION ↔ Cargo.toml）。

**遗留张力（S95 已拍板并落地，选①）**：fs_stat/fs_list 这类微秒级操作走 exe 子进程，热态
p50≈8-10ms 已贴 <10ms 预算地板（裸 exe 拉起 p50≈30ms、纯 os.stat 0.008ms——
进程拉起溢价三个数量级）。S95 拍板方案①：fs_read/fs_stat/fs_list 回迁纯
Python（fs_write 仍走 rx-fs.exe），等价性由 golden master oracle 锁定（§7），
复测 fs_stat p50 0.3ms（余量 ~30 倍）。本节延迟表保留为 v2.20.0 历史基线。

## 7. S95 回迁实测 + 高压电池 + Linux 面 + H2 首测（2026-09-08，v2.21.0）

**回迁等价性（golden master oracle）**：回迁前 bench/s95_fs_golden.py 以现行 exe
薄壳捕获 40 场景（universal newlines / utf-8 边界 / 1MB 门 / 幽灵路径 / 深度钳 /
排序混合 / 六层树 / 沙盒拒绝…；脱敏：根路径→@T、mtime→@int、error_detail→@tb、
超 4K content→sha256）入 tests/fixtures/s95_fs_golden.json——**fixture 只捕一次，
回迁后不重捕（防自证）**；tests/test_s95_fs_back_contract.py 重放同一矩阵断言
逐字段全等。回迁前对 exe 跑绿=装置自检，回迁后再跑绿=exe↔纯 Python 等价实证。

**延迟复测（bench/s94_perf.py @ v2.21.0）**：
| 工具 | 回迁前热态（§6，v2.20.0） | 回迁后 min/p50/max | 判定 |
|---|---|---|---|
| fs_stat | 7.0/7.7-9.7/13.9 | 0.2/0.3/0.5 ms | PASS，余量 ~30 倍 |
| ast_scan(100f) | 49/50-55/65 | 32.8/33.7/34.7 ms | PASS |
| engine_query | 33/34-40/52 | 24.1/24.7/24.8 ms | PASS |
| code_search | 33/35-43/73 | 22.8/24.1/25.5 ms | 台账量级保持 |
| code_semantic | 591/610-725/767 | 413/422/593 ms | 观察值 |

内存 27.8MB → 15 轮 soak Δ+0.1MB（无泄漏信号，与 §6 基线一致）；留档
bench/results/s94_perf.json（滚动历史）。

**高压电池（tests/test_s95_stress.py，8 测全绿 ~2.3s）**：多线程 read/stat/list
一致性；**8 线程同靶并发写原子性**（320/320 ok——本轮实锤并修掉 sandbox.rs
resolve 级缺陷，见 VULN-HUNTING S95 注记）；写入翻涌下 list 有序完整（含
getsize 瞬时失败 size:-1 契约）；2000 文件语料**经 registry 出口钳制契约**逐页
cursor 续读拼回全量对账（每页 MAX_RESULT_ITEMS=200 + total_items/truncated/
next_cursor，fs_list 的 entries 在结果顶层走 S10 分页而非嵌套标记）；8MB 双门
拒收；30 层深树拒深；100×512KB 读翻涌。

**Linux 面（WSL2，Linux 6.18 / Python 3.12.3）**：tests/test_s95_linux_smoke.py
（纯 Python 臂在无 exe 环境全额工作 + fs_write 缺 exe 报清晰错误不静默降级）+
test_s95_stress.py（7 过 4 跳，4 跳均 exe 门控 skip）+ selftest 双态：无沙盒 →
fs_stat 探针 fail-closed"路径越界（沙盒外）"；UNIFIED_RX_SANDBOX 指仓库 →
FS_STAT ok:true。沙盒纪律两侧等价在 Linux 面成立。

**H2 幻觉守卫一致率首测（bench/h2_guard_eval.py，L3 双臂答案复用，零 API
成本）**：1418 条 file 声明，guard 判定 ↔ 文件存在性真值一致率——A 臂 652 条
wide/strict 均 **1.0**；B 臂 766 条 **0.9295**。分歧全单向：漏判（文件不存在却
放行）= **0**；不一致 ~54 条均为"文件存在但行级引用被驳斥"的严判形态
（guard 行号语义比存在性真值更细，非守卫缺陷）。留档
bench/results/l3/h2_report.json。

## 8. H1-H4 指标台账（S97 归档；数据源在仓、可复算）

四指标定义见 §1/§2。本表只回答三件事：现在测到多少、数据在哪、还差什么。
复算入口：H1 = bench/results/l3/summary.json（330 份双臂原始答案在
bench/results/l3/{A,B}/）；H2 = `python bench/h2_guard_eval.py`；
H3 = `python bench/h3_score.py`；H4 = bench/results/l3/h4_lessons.jsonl + ROUNDLOG S20。

| 指标 | 最新实测 | 数据/入口 | 成本 | 缺口 |
|---|---|---|---|---|
| H1 任务增益 | Δsolved **+6.67pp**（deepseek-chat 23/90→29/90，n=90/90）；**+10pp**（glm-4.5-flash 0/90→5/50，**臂 n 不对称**如实标注）。代价侧：轮次 1→12.44、input 165→26487 tok/任务、成本 $0.0661→$0.789（12×）、墙钟 6.2→15.8s | l3/summary.json（S14 双臂） | 已花 | A/B 复跑需 API 预算 |
| H2 守卫一致率 | A 臂 652 条 **1.0/1.0**；B 臂 766 条 **0.9295**；漏判（不存在却放行）**0**；分歧全为行级严判 | bench/h2_guard_eval.py（S95 首测，S97 复算同值） | 零 | — |
| H3 扫描器查准 | api_key_sk tp=6/n=6 **precision 1.0**；panic_family/private_key_block/secret_by_key 各 n=1（WEAK 黄灯）；FP 复检 eval_exec **0 命中**（案底 FP=10 保持修复） | bench/h3_score.py（S18 首测，S97 复测 PASS） | 零 | 3 规则样本量不足（需扩标注库） |
| H4 记忆复利 | 8 个全败任务注入教训复跑：solved 0/8→3/8、fail 点 18→5（-72%） | h4_lessons.jsonl + ROUNDLOG S20（缩影，n 小） | 已花 | 复跑需 API 预算；样本量待扩 |

**H1 口径校正（如实）**：H1 假设原文"省轮次省 token"与 L3 实测方向相反——工具臂
的轮次/token/成本显著高于裸模型（裸模型单轮直答，答错也便宜）；L3 支持的是
**解决率增益 + 可核验性**（S14：裸模型文件引用存在率 0%，工具组 63%/23%）。
即工具的价值在"做对且可核查"，不在"省 API 开销"；后续 A/B 复跑按此口径归档。

**S97 顺带清账的两个纪律/测量问题**：①`ast_scan` 从未过沙盒（S88 普查漏网——
S84 已拆独立文件，不在 scan.py 名单内）→ 已加门 + 回归；②`hallucination_guard`
读文件数行（读原语）未过沙盒 → 沙盒外声明落 unverifiable（fail-closed）；
③`h3_score.py` 的 FP 复检曾把"探针被拒"与"真 FP 回归"混为一谈（found=-1 →
pass:false 假红）→ blocked 语义分离；bench 脚本显式声明沙盒（s94_perf 同纪律）。

