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
  5. 全量 pytest + bench dry-run 门禁（既有项保留）；
  6. **rust job**：cargo test + clippy -D warnings（双绿纪律的 Rust 侧进 CI）；
  7. **机器局部配置的 CI 覆盖**（S125 补，首跑 CI 实锤）：仓库根
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
- **scan.yml**（每周一 03:23 UTC + 手动）：secrets_hunt 全仓周扫。
- 边界（诚实声明）：CI 只扫工作树；**历史提交不在 CI 扫**（新推送由 GitHub
  push protection 兜底；改史治理走第 4 条泄漏响应顺序）。

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
   当前候选序：**secrets_hunt**（正则+IO 双热区，收益预期最大）→
   **ide_dead_code**（pyast.rs 节点面已就绪，ast 全库遍历是 Python 热区）；
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
- secrets_hunt 原生化（候选序第一，需先测原生基线再动）；
- ide_dead_code 原生化（复用 pyast.rs；死码判定的作用域逻辑进 bug.rs 同层）；
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
- **审计欠账**：Mimosa scanner_enobufs 未收敛，copy-based 全审计待还——
  在还清之前任何文档/输出都不得宣称"项目安全"。
