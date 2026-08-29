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
| 延迟预算 | fs_* <10ms；scan 全仓 ≤2s/100 文件；engine_query ≤15s（含 BM25 降级） | 计时断言 |
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

