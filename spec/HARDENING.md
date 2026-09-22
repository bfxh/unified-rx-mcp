# 加固纪律（HARDENING）

定位：把"惨案"变成红线的流程文档。触发案底：**S123 推送被 GitHub push protection
拒收**——测试夹具里的假凭据以完整字面量入库，GitHub 分不清真假；同轮 Mimosa hook
已拦过一次。同一课连中两枪，说明这是流程缺口不是运气问题。本文档只立红线和实测
事实，不替任何扫描工具背书"项目安全"（Mimosa 审计欠账未还）。

## 一、明文红线（S124 盘点实锤为基线）

1. **真凭据不入库**：任何真实 API key/token/密码不得出现在代码、测试、文档、
   配置、commit message、issue 里。真凭据只放环境变量或本机 config，且该 config
   不得位于任何 git 仓库内。
2. **现状实锤（2026-09-14 盘点）**：
   - 本仓（unified-rx-mcp）当前树 + 全部 git 历史（`git log --all -S` 对
     config 值前缀实查）：**零真实凭据**；
   - Yan Agent 配置（`AppData/Roaming/yan-agent/YanData/config.json`）10 个
     secret 形字段**全在本机 AppData，向上无任何 .git——物理上进不了 GitHub**；
     该目录本身也不是 git 仓库（无远端）；
   - 仓内 0 个被跟踪的二进制/构建产物（rust/target 不入库）。
3. **待办（本机明文的根治路径，需要你决定后动手）**：轮换上述 key（先到
   供应商控制台换新，旧的作废）→ 新值走环境变量 → config.json 里不再存明文。
   在轮换完成前，文件保持只读本机使用（当前已满足"不进 GitHub"）。
4. **泄漏响应顺序**：先吊销（revoke）再清理；顺序反了等于没清。掩码输出纪律：
   扫描结果只给掩码（前4后2+长度），扫描器本身不能变成二次泄漏源。

## 二、夹具纪律（两道门同判的红线）

- 测试文件里**只允许存凭据碎片**：完整凭据形状必须运行时拼接
  （`"AKIA" + "IOSFODNN7EXAMPLE"` 式），完整形状只活在 tmp_path 的夹具文件里。
- Mimosa hook（写盘侧）与 GitHub push protection（推送侧）分不清假凭据——
  别指望解释，形状本身就是违规。
- 物理键名同样运行时拼（`"auth_" + "token = "`），键名+形状齐了也一样被拦。

## 三、CI 门禁（S124 起，工作流程不许悄悄变弱）

`.github/workflows/` 两份 workflow，形状由 `tests/test_s124_ci_assets.py` 锁死
（谁删步骤谁红）：

- **core.yml**（push/PR 触发）：
  1. pytest 双解释器矩阵 **3.11 + 3.14**（此前只有 3.11——本地主开发解释器 3.14
     没被 CI 覆盖，是盲区）；
  2. **cargo build --release 必跑**：加速工具在 CI 必须真跑，不做静默降级
     （此前 CI 无 exe，EXE_TAG 一直 SKIP，加速面在 CI 上是零覆盖）；
  3. **secrets gate**（`scripts/ci_secrets_gate.py`）：dogfood 自家
     secrets_hunt，critical/high 出测试区即红；先扫再建 target，扫描面=干净树；
  4. **selftest hard gate**（`scripts/ci_gate.py`）：对账行从"提示"升为"退出码"
     ——SCHEMA_BAD 0 / EXE_TAG drift=0 missing=0（SKIP 也算失败）/
     VERSION_TAG OK|NEXT / SKILLS_DOCS stale=0 dead=0；checkout `fetch-depth: 0`
     保真对账；
  5. **self-attack gate**（S138，`scripts/attack_gate.py`）：dogfood
     `attack_cruise`（四靶模糊×12 用例 + 大输入 + 授权门自审含组合透传 + 路径探针）
     verdict 必须 clean；**data-flow gate**（S139，`scripts/taint_gate.py`）：
     dogfood `rust_taint_scan`——产品面（除 bench/）definite 不得超
     `spec/taint-baseline.json` 基线，新增即红（基线 why 字段=人工确认理由，
     占位未填由 test_s139 拦截）；
  6. 全量 pytest + bench dry-run 门禁（既有项保留）；
  7. **rust job**：cargo test + clippy -D warnings（双绿纪律的 Rust 侧进 CI）；
  8. **机器局部配置的 CI 覆盖**（S125 补，首跑 CI 实锤）：仓库根
     `.cargo/config.toml` 的 target-dir 是本机绝对路径（S78 中文路径 workaround）——
     CI 用 `CARGO_TARGET_DIR=%TEMP%\rx-rs-target` 环境变量覆盖（env 优先于 config，
     且只放 build 单步——全局导出会污染 fixture crate 的 cargo），
     让 exe 落在工具查找的位置；selftest 步给 `UNIFIED_RX_SANDBOX=$GITHUB_WORKSPACE`
     让内部 fs 自检在沙盒内跑。三根字符串已入形状锁（谁删谁红）。
- **CI 首绿战记（S125，8 轮 / 14 项缺陷全归档 ROUNDLOG S125 附）**，两条通用教训：
  ①**外部工具输出必须按"最坏环境"解析**——CI 的 `CARGO_TERM_COLOR=always` 给诊断行
  注 ANSI 色码，行式解析器必须先剥色码（本地管道无色 ≠ 可把"本地绿"当充分条件）；
  ②**路径比较必须两侧同函数解析**——CI 的 TEMP 是 junction/symlink 形态，
  canonical 与原始字符串不同形：`strictly_under` 只解析 target 是产品级缺陷
  （违反"沙盒语义两侧等价"），测试里的原始路径断言同理（本地真 junction 回归已入册）。
- **scan.yml**（每周一 03:23 UTC + 手动）：**审计三连周扫**（S139 扩）——secrets_hunt 全仓 + self-attack gate + data-flow gate（含 rust toolchain 与 exe 构建步骤，同 core.yml 纪律）。
- 边界（诚实声明）：CI 只扫工作树；**历史提交不在 CI 扫**（新推送由 GitHub
  push protection 兜底；改史治理走第 4 条泄漏响应顺序）。
- **本地优先（S145，用户指令「把审核搞强点，不需要用 GitHub 和 Linux」；S147 再上强度）**：
  以上全部门禁已收敛为**一条本地命令** `python -X utf8 scripts/local_gate.py`
  （快门 14 步秒级 / 全门 18 步含双测与 clippy），与 CI **同一套脚本**——CI 降格为
  镜像/备份通道，审核在本机（Windows + ZCode）即可完整跑完。`.githooks/`
  版本化钩子（pre-commit 快门、pre-push 全门）经 `git config core.hooksPath
  .githooks` 一次安装；本地门与 CI 的**不漂移**由 tests/test_s145_gates.py 锁死
  （core.yml 出现的门脚本必须在 local_gate 步骤里）；真门验证
  （`UNIFIED_RX_GATE_FORCE_FAIL` 注入必红）入册。另：`taint_gate.py --update-baseline`
  曾把既有 why 全清成占位（实锤）——已修为按 (file,sink) 继承旧 why，只新条目落
  占位（记账动作不许销毁人工结论）。
- **S147 三道上强度的新门**：①**历史明文门** `scripts/secrets_history.py`
  （近 N 提交 diff 面扫红线——树扫与 push-protection 都覆盖不到的历史面；
  tests/ 豁免同树扫口径；dump 落仓内经沙盒校验）；②**依赖红线机器化**
  `scripts/deps_lock.py`（`[dependencies]` 恒空此前只有文档、没有门）；
  ③**审计时效与账本对账** `scripts/audit_ledger.py` + `spec/audit-ledger.json`
  （封印账本机器可读；表↔账本逐字对账；时效 ≤14 天且 ≤60 提交，超期即红，
  `--allow-stale` 显式放行）。① ② 进 CI（自包含），③ 本地门（Mimosa 仪式为
  agent 驱动，CI 无法自愈，避免卡死远端流水线）。

## 四·补、Mimosa copy-based 全量审计台账（S137 起）

**纪律**：Mimosa 深扫只跑**副本**（git 工作树拷贝/TEMP），绝不自扫宿主。

**复审仪式（S138 起脚本化，tag 前执行）**：
1. `python scripts/audit_copy.py <副本路径>`（副本+记账行：files/head；硬拒仓内）；
2. Mimosa 深扫该副本（MCP：security_scan_start depth=deep），拿 seal 与新版 report.md；
3. `python scripts/audit_diff.py <旧 report.md> <新 report.md>`——**新增非空即红**
   （回归需人工过目；`--allow-added` 降级只报告）；差量与双 seal 记入上表。
CI 侧对应的自动化门 = **Self-attack gate**（`scripts/attack_gate.py`：attack_cruise
判定必须 clean——覆盖四靶模糊+大输入+授权门自审含组合透传+路径探针；core.yml 硬 step）。

| 轮次 | 副本 | 封印 | 条数 | 运行状态 | 差量 |
|---|---|---|---|---|---|
| 首轮 | TEMP/s137_audit_copy | sha256:7890b599… | 59 | **inconclusive**（工具侧覆盖缺口） | — |
| 修复 | local_run shell=True→argv（实锤） | — | — | — | — |
| 复审 | TEMP/s137_audit_copy2 | sha256:3159dfad… | 57 | inconclusive | **唯一差量=修掉的两条**，零新增 |
| 复审二（S143 后） | TEMP/urx-audit-copy @4eae149 | sha256:426d0a37… | 57 | inconclusive | added=0；gone=2（均为已修的命令注入，对照的是修复前基线——S137 首轮报告留档） |
| 复审三（S144 后） | TEMP/urx-audit-copy-s144 @6e1a837 | sha256:dc2b9a97… | 57 | inconclusive | **added=0；gone=0（零漂移）** |

**复审三记账（S144）**：副本 782 文件 / head=6e1a837（含 B3 注入前缀逻辑、
`bench/tool_evals.py`、toolmeta 注入清单与 13 条瘦身描述）；深扫 10s；差量与
上轮**完全一致**（57/57）——本轮回的改动面零新增零消失。运行状态照旧
inconclusive，**不据此宣称"项目安全"**。

**复审二记账（S143）**：副本 778 文件 / head=4eae149（S143 提交，含 toolmeta.py、
toolface_budget.py 新面）；深扫 9s 完成；差量**零新增**、白名单外无漂移——
S140-S143 的改动（breaker/burnwatch/annotations/仪表）未引入新发现。运行状态仍
**inconclusive**（覆盖缺口未消），**不据此宣称"项目安全"**，继续挂账。

**首轮 59 条分类**（产品面 7 处 + bench 面 45 + 冻结快照 6 + 杂 1）：
- 产品面：`local_run` 命令注入 ×2 = **实锤修复**（shell=True 与契约矛盾→argv
  直传，非 posix shlex+剥引号防 Windows 反斜杠）；`ide_debug.py:171` eval=条件
  断点设计内（授权门后）；`ide_debug.py:272` 固定 tempdir 落点=FP；
  `vulnkb.py:137` 知识库文本字符串=FP；`ide_edit`/`lsp_actions`/`learn`/`metrics`
  ×6 = **沙盒门在数据流上游**（`_fs_resolve` 已核实，扫描器无数据流视角）；
  `game.py:43`=本机固定 127.0.0.1 端点（仅端口参数），SSRF moot。
- bench 面 ~45 条（路径穿越/SSRF/弱随机）=开发夹具面（非工具面发布面），
  记录接受；`manual_snaps` 6 条=冻结历史快照（零引用≠可删，S126 已定）。
- **欠账状态（如实）**：四轮副本审计已执行（S137 两轮 + S143 复审二 + S144 复审三）、
  封印在案、复审保持收敛（59→57→57→57，差量全为修复项或零漂移）；但四次运行都被
  工具标记 **inconclusive**（其覆盖缺口未消），**不据此宣称"项目安全"**——欠账
  部分收敛、继续挂账，复审时机=每次 tag 前。

## 四、已知良性基线（本仓 secrets_hunt 自扫，2026-09-14）

725 文件 / 18 命中 / 零红线（critical/high 全在 tests/ 豁免区）：
- `tests/test_appaudit.py`：PEM×2 + AWS 假值——**已推送进远端历史**的旧内容，
  合规假值（历史内容不在 CI 扫描面）；
- `tests/test_s123_deadcode_secrets.py`：运行时拼接碎片的熵层 suspect——豁免区；
- `bench/results/*.json`：SWE 语料数据（medium/suspect）；
- `spec/ROUNDLOG.md`：熵校验哈希实录（suspect）；
- `tools/astgrep.py` / `tools/meta.py`：字符白名单长串（熵层经典误报）。
基线漂移的处理方式：**修源头，不放宽门禁**。

## 五、性能战略（"原生要等，优化不能等"）

现状基线：secrets_hunt 纯 Python 全仓 725 文件 **1362ms（≈1.9ms/文件）**，
其中大头是逐文件读盘 + 正则引擎开销。路线按性能基线纪律走：

1. **Python 侧先优化**（不等 Rust）：算法/IO 层的便宜优化先行（单遍读、
   批量 read、正则预编译已是现状；下一步可测 mmap 大文件路径）；
2. **Rust 化按实测收益排队**：先测原生基线，实测赢才进 auto（S84/S90 纪律）。
   ~~secrets_hunt~~（**S134 已兑** 3.5×）→ ~~ide_dead_code~~（**S135 已兑**：
   pyast 加 deco 字段承载装饰器判定，夹具 + 真仓双对照逐字节等价，
   0.51s→0.23s 2.2×）——**§六 Rust 候选到此清零**（astscan/bugscan 早已全薄壳）；
3. **file_scan 熵层已有 rust/gpu 双档**（auto 已接），保持；
4. 依赖红线不变：Cargo `[dependencies]` 恒空，零第三方 crate。

## 六、缺口盘点（S125 时点，按优先级）

> **S126 文档先行轮 → S127 实施轮一**：整合除重 / 上帝对象拆分 / IDE 升级的分项方案
> 与实施排序见 [spec/CONSOLIDATION.md](CONSOLIDATION.md)（基于 dogfood 实测证据：
> 调用图 fan-in/out 榜、死代码核验、模块稳定性榜；§1.4 含"S126 量尺缺陷"误诊更正——
> 全仓复扫证明 ide_deadcode 引用模型本就数 Attribute，假阳性系扫描范围错误）。
> S127 已兑：P0 scan.py 拆分（→ scan + tools/code_review.py）+ C1 遍历除重
> （tools/filewalk.py 唯一 os.walk）+ 死代码清理 + 三处既有门连锁修复；余项按其
> §五 顺序逐项过门禁。

**Rust 侧**：
- ~~secrets_hunt 原生化~~（**S134 已兑**，见 §五）；
- ~~ide_dead_code 原生化~~（**S135 已兑**：独立 deadcode.rs 复用 pyast.rs）；
- astscan/bugscan 已全薄壳，无动作。

**IDE 域**：
- ~~**真调用图**~~（**S125 已兑**：`ide_callgraph` —— 符号级调用边 + callers/callees
  遍历 + 环检出，同一 nameres 作用域引擎 + 预扫描种子，见 spec/CALLGRAPH.md。
  未解析率如实入档：本仓 tools/ 30 文件 resolved 421 / unresolved 1653；
  已知边界=无类型推断（receiver_var 为主因）与外部依赖（external））；
  下轮候选：ide_impact 的调用面补充（引用 vs 调用分层不混）、ide_dead_code
  接"零调用"辅助证据；
- **类型检查集成**：mypy/pyright 未接；零第三方依赖红线下只能走"外部工具
  探测 + 结果结构化"（能力探测失败如实降级，同 pylsp 模式），待设计；
- **覆盖率趋势**：code_coverage 有单点，缺跨轮趋势存档与回归对比；
- **审计欠账（S137 更新）**：copy-based 全量审计已执行两轮（见 §四·补台账，封印在案），复审收敛（59→57，唯一差量=修复项）；**工具标记 inconclusive 仍未消**——欠账部分收敛，继续挂账；在收敛/结清前任何文档/输出都不得宣称"项目安全"。

**外部对标（S142 新组）**——联网对标全文 [spec/EXTERNAL-ALIGNMENT.md](EXTERNAL-ALIGNMENT.md)
（四块坐标 + 实测对标 + 精选三档待办，不堆）：
- **协议线落后四代**（我们钉 2025-03-26；最新 2026-07-28 无状态化大改：删 initialize
  握手、server/discover、resultType 必带、MRTR、JSON Schema 2020-12）——升级有宿主
  决策点，先探 ZCode/Yan Agent 支持线（B1 挂账）；
- ~~**annotations/title 未发**~~（**S143 已兑**：核对规范原文后更正——字段自
  2025-03-26（我们钉的版本）即有，纯属漏发；`toolmeta.py` 72 件中文标题 +
  `registry.list_tools` 发 `annotations`，行为提示与三档授权同口径）；
- ~~**工具面定义摊派**~~（**S143 已兑**：`scripts/toolface_budget.py` 进 CI 硬门
  + 真门测试；摸底 **38,119 字符 ≈ 12.7K token**（注解自身 +5,972），软帽
  45,000 超帽即红——瘦身位（B4 语种账 / 头部单件）继续挂 B 档）；
- ~~**间接注入无立场**~~（**S144 已兑**：14 件内容类工具声明
  `toolmeta.UNTRUSTED_OUTPUT_TOOLS` + 协议回包 `[untrusted-content]` 前缀 +
  skills"工具输出纪律"；顺带修复 tools/list 不转发 annotations 的 S143 补遗）；
- ~~**任务级工具评测（evals）缺**~~（**S144 已兑**：`bench/tool_evals.py` 13 任务
  × calls/errors/chars，基线对照进 CI；sabotage 真门验证）；
- ~~**描述瘦身与语种账**~~（**S144 已兑**：13 条 −44%，机器面向保持中文的结论
  与瘦身纪律入 EXTERNAL-ALIGNMENT B4）。

## 七、授权三档（S132 立文；兑现 DESIGN-REVIEW H1）

工具注册按**动作性质**自证分档——`auth_gate_sweep` 端到端复核（含组合透传项）：

| 档 | 判据 | 门 | 代表工具（非穷举） |
|---|---|---|---|
| ① 纯读 | 只读文件/目录，路径过沙盒（`_fs_resolve` fail-closed） | 不挂门 | fs_read/fs_stat/fs_list、locate_edit、ide_outline、检索三件、bug_scan/std_check/ui_check、code_review |
| ② 自有只读扫描 | 跑**本仓自带 exe** 扫描代码/文件，不改任何状态 | 不挂门；exe 缺失清晰报错不静默降级 | scan 域五件、ast_scan、filescan、near_dupes、secrets_hunt、ide_callgraph、ide_risk_rank、rust_taint_scan、module_stability（只读 git log） |
| ③ 执行·写 | 跑构建/测试/调试/用户代码、起进程/起窗口、写盘、改状态 | **必挂 `requires_auth`**；写盘动作再在 handler 内自查 `__authorized`；组合工具内层**字面量**透传 | ide_build/ide_test/ide_debug/ide_break/ide_diagnostics（S130 归位）/ide_doctor/ide_multi_check/ide_edit_multi/ide_batch_edit/ide_vscode、local_run/process/backup、fs_write、app_clone/app_clean、code_coverage、blender_verify |

**组合透传纪律**（S132/H3 起有常驻检查器）：组合工具内层
`registry.call("<挂门工具>", …)` 的参数须**字面量**携带 `__authorized`——外层门已
确认授权，内层是透传不是绕过；靠包装器隐式注入不算数（静态看不见）。检查器=
`auth_gate_sweep`「组合透传」项，扫包内 tools/ 与 bench/，首跑即抓出
`agent_selfcheck.py → app_clone`、`swe_repair.py → ide_break` 两处真缺口
（被门拒后静默退化，同 S130 病灶型）——已修并回归。

**边界（明示）**：沙盒钳制**只管本进程的文件访问**；③档的子进程（cargo/go/dlv/
用户脚本）天然可越沙盒触盘——这是"执行用户代码"的固有面，由授权门（人的确认）
兜底，不是沙盒遗漏。新工具注册时按上表自证，③档缺字面量透传即被检查器拦。
