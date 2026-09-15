# Reasonix 审核强化套件（提案，S147）

来源：`D:\开发\unified-rx-mcp`（unified-rx 工具箱）在 S145/S147 两轮把"审核
本地化 + 上强度"跑通后的**可移植版本**。目标：**合入前的最小完整审核，一条命令、
本机可跑、可判红**——CI 继续做镜像/备份，不再承担"唯一把关人"。

## 三件套

| 文件 | 干什么 | 装到哪 |
|---|---|---|
| `local_audit.py` | 一条命令跑完 9 步门（见下）；`--fast` 秒级快门、缺省含 `go test ./...` | `scripts/local_audit.py` |
| `pre-commit` | 提交前跑快门（`--fast`） | `.githooks/pre-commit`（你们已有 `.githooks/`，`make hooks` 会装） |
| `Makefile.snippet` | `make audit` / `make audit-fast` 目标 | 合并进现有 Makefile |

## 门清单（`python3 scripts/local_audit.py --list`）

| 步骤 | 判红条件 | 备注 |
|---|---|---|
| repolint | 仓内 `tools/repolint` 报错 | 你们已有，保留为第一步 |
| vet | `go vet ./...` 非零 | |
| golangci | lint 非零 | 未安装 → SKIP（不静默当通过） |
| secrets | 工作树出现明文红线（AWS/GitHub/Slack/Google/Stripe/PEM/JWT 模式） | tests/docs/site 等豁免面照你们现状 |
| history | 近 50 提交 **diff 面**出现红线 | 树扫与 push-protection 都覆盖不到的历史面 |
| deps | `go.mod` require 出现基线外新增（含 sdk/desktop 子模块） | 新增依赖=记账动作，`--update-deps` 显式刷新 |
| bigfiles | 源文件 >5000 行或 >300KB | 现状 `desktop/app.go` 383KB 会红——这正是推动拆分的入口 |
| freshness | 审计账本（`.audit-ledger.json`）超过 14 天或落后 60 提交 | `--allow-stale` 显式放行 |
| test | `go test ./... -count=1` 非零 | 慢档（全门才跑） |

## 建议的接入顺序（低阻力→高收益）

1. `scripts/local_audit.py` 落库 + `make audit-fast`（先只读不拦：跑一周看噪音）；
2. `.githooks/pre-commit` 挂快门（`make hooks` 时一起装）——提交前 10 秒级拦截；
3. 把 `bigfiles`/`deps`/`secrets` 三步的阈值按现状**先放宽**（`--update-deps`
   建基线、`desktop/app.go` 加白名单并写 why），再逐步收紧——门要能长期开着，
   先别一步到位；
4. `freshness` 配合副本审计：每轮发布前对**工作树副本**跑外部深度审计（不扫
   宿主），封印（seal）与条数记进 `.audit-ledger.json`；
5. 可选：`history` 窗口从 50 提到 500（一次性把历史面清一遍）。

## 与你们现有体系的差异（为什么值得加）

- **现状**：`.githooks/pre-push` 只跑 `go vet` + `repolint`；明文/依赖/大文件/
  时效没有本地门；CI（28 条 workflow）很全但**只能事后拦**，且依赖远端可用性。
- **加法**：把"明文 / 历史明文 / 依赖面 / 上帝文件 / 审计时效"五类**本地化**，
  推送前就红；CI 完全不用改。
- **哲学**（与 unified-rx 一致）：门要**可判红**（每条都有负例测试）、要**有账本**
  （抬阈值=记账动作）、要**不依赖特定 OS**（纯 stdlib Python 3 / Go 工具链）。

## 注意

- `local_audit.py` 是**提案**，不是补丁——请按你们代码规范调整后再落库；
- 阈值常量在文件头（`MAX_FILE_LINES` 等），改阈值请写进 commit message；
- 本套件零第三方依赖（与你们 `Makefile` 的 `lint-install` 外置工具链互不冲突）。
