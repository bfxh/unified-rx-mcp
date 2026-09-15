# 外部审计：esengine/DeepSeek-Reasonix（S147，2026-09-15）

> **审计对象**：`https://github.com/esengine/DeepSeek-Reasonix`（公开仓，MIT，
> Go+TS，35.5k star，默认分支 `main-v2` @ `b21deef`）。
> **授权与边界**：只读审计（API 全树清单 + CI/配置/钩子文件实读 + 抽样深扫）；
> **未运行其代码、未改动其仓库、未执行任何写操作**。
> **免责**：本文是窗口内的静态观察，不是安全背书——"未发现"≠"不存在"；
> 我方扫描器对 Go 的覆盖有限（见 §4 覆盖边界）。

## 1. 方法与证据

| 面 | 手段 | 规模 |
|---|---|---|
| 仓库盘点 | GitHub tree API（`recursive=1`） | 7,139 文件（.go 4039 / .ts 929 / .tsx 443 / .py 419） |
| 治理面实读 | workflows/ci.yml（69KB）、codeql.yml、Makefile、.golangci.yml、.githooks/pre-push、SECURITY.md | 28 条 workflow |
| 明文面 | 我方 `secrets_hunt`（工作树全量，Rust 引擎） | 9,000 文件上限，命中 200（分级见 §3） |
| 重复面 | 我方 `near_dupes`（internal/，threshold 0.85） | 2,000 文件 → 3 簇 |
| UI 面 | 我方 `ui_check`（desktop/frontend/src） | TSX 无覆盖（如实记录，见 §4） |
| 规模面 | 源文件行数统计（>200KB 全量精算） | 上帝文件榜见 §3 |

## 2. 现状评分（强项先说）

**已达业界上游的部分**（不是客套，都有文件实证）：
- **测试体量**：1,999 个 `*_test.go`；CI 里 `-race`、`-coverprofile`、平台化 smoke
  （macOS/Windows/ConPTY/Electron）齐备；
- **供应链**：`govulncheck ./...` 进 CI；golangci 版本钉住（`.golangci-version`）；
  dependabot 在册；`go.sum` 全量提交；
- **自有工具链**：`tools/repolint` 仓内检查器 + `make hooks` 安装 `.githooks`；
- **运行时防护意识**：`internal/secrets/`（脱敏模块，含成套 redact 测试）——
  说明"密钥进日志"这条已在设计层被想过；
- **发布工程**：release-stable / release-desktop / release-npm / 签名/公证校验
  独立 workflow，release-notes 流水线化；
- **安全文档**：SECURITY.md 在册。

**缺口（本次要提的）**：

| # | 缺口 | 证据 | 影响 |
|---|---|---|---|
| G1 | **无静态明文扫描**（工作树/历史都不扫） | ci.yml 全文无 gitleaks/trufflehog/等效步骤；CodeQL 不做 secrets | 明文入库只能靠事后人工发现 |
| G2 | **本地门只到 vet+repolint** | `.githooks/pre-push` 仅两行；无 pre-commit | 明文/依赖/大文件问题要等 CI（远端、分钟级）才发现 |
| G3 | **上帝文件无门** | desktop/app.go **11,982 行**、app_test.go 10,414、tabs.go 8,058、SettingsPanel.tsx 7,296、controller.go 6,054、bridge.ts 5,709、useController.ts 4,950 | 单文件改动爆炸半径大、评审成本高、并行冲突频繁 |
| G4 | **无审计封印/时效账本** | 仓库无 `.audit*`/深扫记录 | "多久没做外部深扫"没有客观信号 |
| G5 | **覆盖率无阈值门** | 有 `-coverprofile` 产出，未见阈值判定 | 覆盖率只被记录、不被要求 |
| G6 | 依赖面只被"解析"不被"记账" | go.mod 变更随 PR 走，无白名单/基线对账 | 新依赖引入无独立复核点（与 G1 同类：变更可见性） |

## 3. 本次实测（数字与分类）

**明文面（secrets_hunt，200 命中）**：critical 4 / high 2 / **medium 194**。
逐条核验红线 6 条——**全部为误报**，且成因很有代表性：
- 4× critical = `-----BEGIN PRIVATE KEY-----` 字面量，出现在
  `workers/crash-report/src/firebase_rtdb.ts:94` 的**脱敏代码**
  （`.replace("-----BEGIN PRIVATE KEY-----", "")`）与
  `firebase_crash_integration.test.ts:62` 的**测试构造**；
- 2× high = `internal/secrets/redact_test.go:17,25` 的
  `xoxb-123456789012-abcdefabcdef`——**脱敏模块自己的测试夹具**；
- medium 抽查（`.github/workflows/deploy-crash-worker.yml:87` 的 env 打印、
  `desktop/app.go:10306` 普通标识符行）均为熵/关键词偶然。
→ **结论（有边界的说法）**：在本窗口与本扫描器口径下，该仓**零明文真阳性**；
  红线命中全部落在"脱敏器与测试"这两类经典误报面上。这不构成安全背书。

**重复面（near_dupes）**：3 簇，样本均为
`internal/{boot,bot,botruntime,...}/main_test.go` 的同构测试骨架——低危，
但如果包模板可参数化，可作为后续收敛点。

**规模面**：>200KB 源文件 10 个（其中 3 个是 locales 翻译数据，属正常），
其余 7 个即 §2 G3 的上帝文件榜。

## 4. 覆盖边界（我方扫描器在此仓的真实能力）

- **能用**：明文（Rust 引擎，语言无关）、近似重复、规模统计；
- **弱/不可用**：Go 的缺陷规则、污点数据流、UI 检查——我方 `bug_scan`/`rust_taint_scan`/
  `ui_check` 面向 Python/JS/Rust 生态，对 Go **无有效覆盖**（ui_check 对
  TSX 返回 0 文件——是"没覆盖"，不是"没问题"）；
- **因此**：Go 侧的代码质量与漏洞面，专业工具是 **golangci-lint（建议扩 linter 集）
  与 gosec / govulncheck**——本报告不越界替它们下结论。

## 5. 审核强化提案（按"收益×低阻力"排序）

配套套件：`docs/reasonix-audit-kit/`（`local_audit.py` / `pre-commit` /
`Makefile.snippet` / README——零依赖、可直接落库、按你们规范再改）。

1. **P0 明文双门**（补 G1）：`local_audit.py` 的 `secrets` + `history` 两步
   （工作树 + 近 50 提交 diff 面）。落地成本≈零依赖 Python；阈值/豁免面按你们
   `internal/secrets` 的现状先对齐；
2. **P0 pre-commit 快门**（补 G2）：`.githooks/pre-commit` 跑
   `local_audit.py --fast`（vet/repolint/lint/明文/历史明文/依赖/大文件/时效，
   秒级）；`make hooks` 一并安装；
3. **P1 上帝文件 ratchet 门**（补 G3）：**先冻结、后削减**——把现状
   （app.go 11982 行等）记为基线，门只拦"变大"；每次拆分 PR 往下调基线。
   收益：爆炸半径与评审成本是可度量的下降曲线；
4. **P1 依赖记账门**（补 G6）：`go.mod` require 基线 + 新增即红（`--update-deps`
   显式刷新并写进 commit message）——把"引入新依赖"变成显式决策；
5. **P2 审计时效账本**（补 G4）：每轮发布前对**工作树副本**做一次外部深扫，
   seal/条数/head 记 `.audit-ledger.json`；`freshness` 步在超期/超距时提醒；
6. **P2 覆盖率阈值**（补 G5）：在既有 `-coverprofile` 之上加"不得低于基线 −0.5%"
   的趋势门（先记录趋势，再上阈值，避免一次性卡死历史欠账）。

**接入顺序建议**：1+2 先合并（当天可用）→ 一周观察噪音 → 3+4 上 ratchet
（先冻结）→ 5+6 随发布节奏接入。

## 6. 与 unified-rx 现行体系的关系

本报告与套件是把 unified-rx 的 S145/S147 实践（本地一条命令、门可判红、
阈值=记账动作、审计封印+时效）**移植到 Go 语境**的产物；unified-rx 自身的
对应实现在 `scripts/local_gate.py`、`scripts/audit_ledger.py`、
`scripts/secrets_history.py`（S147 三条新门同名同构）。
