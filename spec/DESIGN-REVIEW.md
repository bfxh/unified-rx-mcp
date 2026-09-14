# DESIGN-REVIEW.md —— 工具面设计性评审（S131，70 工具 / 12 组）

> 触发：用户「搞完之后你看看整个 MCP 的工具有什么设计性的问题」。
> 口径：**设计性问题 = 一致性 / 分类 / 权限模型 / 接口形状 / 边界与演化**——
> 不做功能清单（那在 README 与 PANORAMA）；每条给证据（registry 实测 + file:line）
> 与建议（改 or 记录决策）；不重开已结案项（误诊撤销见 CONSOLIDATION §1.4）。

## 〇、盘子总览（registry 实测，2026-09-14 @ v2.48.0）

| 面 | 数 | 备注 |
|---|---|---|
| 工具 / 组 / 实现模块 | 70 / 12 / 36 | 36 模块摊 70 工具——模块化良好，无单文件堆工具 |
| 挂门（requires_auth） | 18 | 执行/写盘类；详见 H1 |
| 手动门（manual_gate） | 1 | `ide_lsp`（读开放、rename_apply 内自查） |
| 主路径参数词 | `path`×28 / `root`×15 / `file`×4 | 见 H2——**同一语义两种词汇** |

## 一、高优先（建议尽快处理，均有实际代价）

### H1 授权分级规则**未成文**——S130 的 clippy 透镜之死就是它的学费
现状：18 件挂门、1 件手动门，但"什么算执行类"从没写成规则。边界因此靠直觉：
`bug_scan` 跑自有 exe 不挂门（对）；`module_stability` 跑 `git log` 不挂门（对，
只读）；`code_review` diff 模式跑 git 不挂门（对）；`ide_diagnostics` 跑
cargo/ruff **曾经不挂门**（错——S130 实锤：clippy 透镜被授权门拒绝又被静默吞，
`engine=none, total=0` 生产路径长期假死）。
建议：落一页三档规则进 skills/README（或 HARDENING）——
①**纯读**（读文件/目录）：不挂门，过沙盒；②**自有只读扫描**（跑本仓 exe 扫代码，
不改状态）：不挂门（exe 缺失清晰报错）；③**执行/写**（跑构建/测试/调试/用户代码、
写盘、起进程）：**必挂门**，写盘动作再加 handler 内 `__authorized` 自查。
`ide_diagnostics` 归③（S130 已改）；`code_coverage` 归③（跑用户脚本，已挂）；
新工具注册时按此表自证。

### H2 参数词汇分裂：`root`(15) vs `path`(28) 同义两词
证据：`ide_callgraph/ide_risk_rank/code_search/code_semantic/repo_map/engine_query/
rust_taint_scan…` 用 `root`；`bug_scan/std_check/ui_check/project_scan/filescan/
near_dupes/secrets_hunt/module_stability/dep_graph/project_health…` 用 `path`。
调用侧（尤其 agent 写 schema 调用）每次都要猜。**拆解**：语义确实有细微分层——
`path` 多为"文件或目录"，`root` 多为"项目根"——但落实不一致（`ide_callgraph`
的 root 也可以是子目录；`filescan.path` 也可以是目录）。
建议：**不改存量面**（70 工具改参数名=大面积破坏面，收益不抵），改为
①在 skills 各域文档开头写统一句："本域路径参：`root`=项目根、`path`=文件或目录、
`file`=单文件"；②**新工具**统一按此三分；③README 加三行词汇表（与
M3 的 kind/engine 词汇表合并成一节）。

### H3 组合工具的授权透传是**约定不是契约**
现状：组合工具（`ide_doctor`/`ide_multi_check`/`ide_diagnostics`）内层 `registry.call`
挂门工具时必须显式带 `__authorized: True`（S130 修 ide_diagnostics、S129 修
swe_repair 两处）。**没有检查器**：下次谁再写一个组合工具漏传，就是 S55/S130
第三次重演。
建议：给 `auth_gate_sweep`（或新检查）加一条静态规则：扫 `tools/*.py` 中的
`registry.call("<挂门工具"` 字符串，若同一 dict 字面量无 `__authorized` →
报"漏透传"。注册表数据已在手（requires_auth），实现小、收益直击历史病灶。
（本轮先记录；实施约半小时，下轮可做。）

## 二、中优先（一致性债，无即时事故但持续摩擦）

### M1 输出键语言分裂：3 件中文键 vs 67 件英文键
证据：`tools/attack.py:210-213`（`auth_gate_sweep` 的 `总工具数/挂门数/挂门清单/
漏拒绝/漏声明/一致性`）；`game_check`、`blender_verify`（tools/game.py）同为
中文顶层键。其余全部英文键。
伤害：消费方（agent/脚本）不能假设键语言；`skipped`/`engine` 等英文约定的对偶
缺席。建议：**新面一律英文键**（中文进值不进键）；存量三件在下次因其他原因
动刀时顺手改并留一版兼容期（或在 skills 标注）。不专程改（收益 < 破坏面）。

### M2 组/模块双轴错位：metrics 三件挂 scan 组；project_health 在 ops
证据：`code_coverage`/`dep_graph`/`module_stability` 实现在 `tools/metrics.py`
（S52 自称"代码质量度量域"）却注册进 `scan` 组；`project_health`（bug/std/ui
评分）在 `ops` 组。S131 已修掉同类样板（breaker meta→guard）。
建议：**暂不动**（组只影响文档聚合与 manifest 分组；改组=计数门全套随动，
收益是纯语义整齐）——但若动，一次做全：新增 `metrics` 组收三件 +
`project_health` 归 scan。留作 C 级批次项（与 CONSOLIDATION §二 B1 一并）。

### M3 一词多义：`kind` 三种口径、`engine` 两种口径
`kind`：bug_scan=实锤级别（definite/clue）｜taint=可达性级别（definite/clue/naive）
｜appaudit=类别。`engine`：impact=降级档（lsp/resolved/text）｜filescan=执行引擎
（rust/gpu/cpu）。各自自洽，但跨域复用同名会误导"跨工具拼装"的调用方。
建议：README 词汇表一节写死三对：`kind`（严重度级别）、`engine`（执行后端）、
`flow`（direct/interproc/cross 污点链）；新面沿用不再发明同名词。

### M4 家族选型表（CONSOLIDATION §二 B1/B2）承诺未落地
四件套（project_scan/project_health/code_review/ide_doctor）与检索三件套
（code_search/code_semantic/engine_query/repo_map）的"什么时候用哪个"说好写进
skills，S126 只是承诺。现状靠 PANORAMA 一表兜着。
建议：补两段进 skills/scan.md 与 skills/search.md（纯文档、半小时）。

## 三、低优先 / 记录在案（不改，属自觉边界）

- **L1 沙盒管本进程不管子进程**：执行类工具（cargo/go/dlv/用户脚本）天然可越过
  沙盒触碰盘面——这是设计意图（授权门兜底），但从未在一处明示。建议下次动
  HARDENING 时加一句边界声明。
- **L2 无 deprecation 机制**：S15 是硬删（证据驱动、无反悔）；个人工具箱可接受，
  若外发需加"deprecated 一版 + 转发"政策。记录。
- **L3 规模复核**：70 工具 / 12 组，本季净增 3（ide_callgraph/ide_risk_rank +
  拆分产物 0），每件都有独立验收与边界声明——"少而准"维持成立，无新噪音。
- **L4 命名动词约定**：现存量基本动词开头（scan/build/read/rank…），无越轨项，
  维持现状即成文惯例。

## 四、结论

高优先三项的共同根因是**规则只活在实现里、没活在文档与检查器里**——S130 事故
（clippy 透镜假死）不是手滑，是 H1+H3 的必然学费。推荐实施顺序：
**H1 立文（半小时）→ H3 检查器（半小时）→ M4 选型表（半小时）→ H2 词汇表
（并入 M3，一次写完）**；M1/M2 因破坏面与收益比挂在"下次动刀顺手"批次。
