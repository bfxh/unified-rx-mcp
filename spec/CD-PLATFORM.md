# CD-PLATFORM.md —— 平台工程路线图（S173，2026-09-25 立文；用户指令）

> 用户原话（2026-09-25）："不仅仅是 CI 还有 CD，把 CD 的标准提高，你自己吸收 git/GitHub
> 代码推送和拉取请求和各种流程……除了 GitHub 其他都一定要本地运行……CI 还要增加持续的
> 健康运行……除了 docker 大部分的东西你直接用 rust 重写然后还要高并发 性能高 验证运行健康，
> CI 甚至还有不同系统 不同版本的适配，还有不同芯片 不同显卡……看看那些大型项目……
> 把黑客工具都融入这个流程，你自己找黑客工具……搞完这些就把查询相关效率 速度都提高……
> 加一个门：任何项目不许为了提速就放弃质量。还有一个后期想法：能清楚其他智能体搞项目的边界。"

## 一、优先级队列（P0 本轮落地 / P1 下轮 / P2 排队）

| 级 | 项 | 状态 |
|---|---|---|
| P0 | **黑客工具链第一件：gitleaks 8.30.1**（业界规则库，本地二进制 `/c/vxl-wl-tools` + CI 钉版下载；`scripts/gitleaks_gate.py` 零容忍 + `.gitleaks.toml` 夹具豁免写理由；金丝雀：植入 AKIA 形状必红） | ✅ 本轮 |
| P0 | **跨平台第一腿：cross-linux job**（cargo test + release 构建；Rust 侧零依赖跨平台干净，Python 腿待梳理路径假设） | ✅ 本轮 |
| P0 | **质量-速度契约成文**（§三）+ 门形态设计（§三.2） | ✅ 本轮成文，门下轮 |
| P1 | **CD：release pipeline**——tag 触发 → 多平台构建（windows/linux/macos × x64/aarch64）→ exe/二进制 artifact + SHA256SUMS + GitHub Release（软件 Supply Chain 对标：sigstore/cosign 签名、SBOM 后续） | 🔜 |
| P1 | **持续健康运行**——每日 health job：真 stdio 握手（mcp-surface）+ selftest + 冒烟（vuln_hunt smoke）+ 资源/延迟基准记录（cli-bench 读数入档）；连续失败自动开 issue（GitHub 是例外出口） | 🔜 |
| P1 | **查询效率提级（Rust 重写第一批）**——先量（semantic/search/resolve 基准入 cli-bench 形状），再落**跨请求解析缓存**（此前判定的设计级改动：PyNode → Arc+Sync 或 Rust 侧 AST），保逐字节输出 | 🔜 |
| P2 | 多版本矩阵扩（Python 3.12/3.13 腿；Rust stable/1.85+ 最低版本线）；aarch64（linux-arm runner 或 qemu）；**显卡矩阵**（OpenCL 探测位已有：CI 无 GPU ⇒ 能力探测降级即"如实报"，GPU 实测留给本机/gpu runner） | 🔜 |
| P2 | 黑客工具后续：osv-scanner v2.6（扫 ci-requirements 依赖漏洞——本仓零依赖，扫的是测试依赖面）、semgrep（结构化安全规则，Python 重，评估）、nuclei/zap（网络面，本地 target-only） | 🔜 |
| P2 | **智能体边界治理**（用户后期想法）：其他智能体在本项目可做/不可做的边界声明 + 机器强制（授权门已有：requires_auth/manual_gate/path_gate——扩成"按智能体身份的边界档"，依赖 S172 的 agent 归因） | 🔜 设计先行 |

## 二、CD 标准提级（吸收大型项目形态）

对标（GitHub Actions 官方仓 / rust-analyzer / ripgrep / bat 的 release 形态）：
1. **构建矩阵**：OS×Arch（windows-x64 先行 → linux-x64/gnu+msvc → macos/aarch64）；
   每个 artifact 带 SHA256 清单；版本与 exe 名单锁步（既有 selftest 已查 EXE_TAG）。
2. **发布流程**：tag `vX.Y.Z` → workflow_dispatch 预演 → Release（notes 自动生成 +
   artifact 附加）；**预发布（pre-release）通道**先行，冒烟过再转正。
3. **PR 流程强化**：PR 事件已扫 merge commit（codeql）⇒ 加 `paths-filter`（rust/ 变更
   只跑 rust 相关 job，省 runner）；PR 模板带"质量-速度契约"勾选项。
4. **本地优先纪律不变**：除 GitHub 侧触发/产物托管外，所有工具二进制本地同版可跑
   （`/c/vxl-wl-tools` 单一来源；CI 只做"同版复验"）。

## 三、质量-速度契约（用户：加一个门，不许为提速放弃质量）

### 3.1 契约正文
**速度不得是质量的债务转移。** 任何以性能为名的改动，必须同时交付三类证据之一：
1. **行为不变证据**（改实现不改行为）：逐字节输出对拍（`sha_faces.py`）/ 金样门 /
   基线棘轮不变——如 astscan 并行 −36% 那批；
2. **行为变化即产品变化**（改行为=新功能/新口径）：走门链 + 金样更新 + 交接档理由登记
   （如 mcp-surface 的未知方法改 error 形状）；
3. **基础设施改动**（门/CI/工具）：金丝雀证明门会红 + 净账不撒谎（god 门按文件数只准减）。

### 3.2 门形态（P1 实现，`scripts/quality_pact_gate.py`）
- 判据一：性能类提交（diff 涉及 `rust/src` 并行段或 bench/）必须携带
  **前后基准对**（`bench/cli_bench.py` 或专用基准的读数，写进提交信息模板段）；
- 判据二：提交信息含 `perf` 前缀时，等价性证据行（`EVIDENCE: sha256 对拍一致` 或
  `EVIDENCE: 行为变更+理由`）必须存在——缺失即红（提交信息 lint，pre-push 档）；
- 判据三：PR 模板勾选项机器校验（GitHub Actions 读 PR body）。

## 四、持续健康运行（P1 细化）
- **每日轻量 health**（schedule）：mcp-surface 真握手 + selftest + exe 版本对账 +
  gitleaks dir——3 分钟内跑完，失败连续 N 次自动开 issue（唯一允许的 GitHub 出口）；
- **每周深度**（scan.yml 已有）：全门 + vuln_hunt 封印 + audit-ledger 对账；
- **运行时健康**：server 启动自检已有（stats maintenance）——加 `health` 工具
  （stdio 会话内自报：版本/exe/账本/最近错误计数），供宿主与 health job 消费。

## 五、反面清单（写下来防止"提速"变借口）
- "先上了再补测试"——不行：门链先绿再合（双绿红线）；
- "并行了就不用对拍"——不行：S169 的 astscan −36% 也是 6/6 逐字节；
- "CI 有 GPU 就不用降级路径"——不行：能力探测降级本身要有测试；
- "agent 归因做了，边界就不用写"——不行：归因是记账，边界是授权，两件事。
