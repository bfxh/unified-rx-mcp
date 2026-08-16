# unified-rx 总体方案（炼化版）

> 2026-08-12 · 基于全仓库实勘 + MCP 生态调研 + 相关项目借鉴
> 目标：把 unified-rx 这个"工具 + MCP + Rust 内核"混合体彻底炼化——
> 现状全貌 / 问题清单 / 技术选型 / 目标架构 / 分阶段路线图，一份文档讲清所有。

---

## 〇、一句话本质

**unified-rx 不是一个 MCP server，是一个"常驻代码智能体内核"**——MCP 只是它的
对外通道（被调用状态），内核里跑着：扫描 daemon（自启动）、Rust 纯函数层（rx-core）、
LSE 教训引擎（Rust）、防幻觉闭环、LSP/索引引导、7 类扩展（24 工具）。

| 层 | 是什么 | 类比 |
|---|---|---|
| MCP 接口 | 被 RX 调用时的入口（小总） | 总线/插座 |
| 工具实现 | 35 核心 + 24 扩展（基线；2026-08-16 已演进 97 + 24 = 121） | 电器 |
| daemon | 7 个常驻循环，开机自启，独立于调用 | 后台电网 |
| rx-core | Rust 纯函数层（25 工具） | 高效马达 |
| LSE 引擎 | 教训进化/UCB 树搜索（Rust） | 记忆+决策 |
| 防幻觉 | 声明验证 + 能力边界 + 回灌 | 保险丝 |

**设计哲学（从代码和文档提炼）**：极简单文件（启动 <100ms / 内存 7MB）+
静态注册表 O(1) 分发 + 懒加载 + 错误隔离 + 常驻。

---

## 一、现状全貌（实勘结果，非文档复述）

### 1.1 三个副本（🚨 最重要发现）

| 副本 | 路径 | 状态 | 说明 |
|---|---|---|---|
| **运行版** ⚡ | `E:\共享\51\unified-rx\server.py` | 2679 行，35 工具，selftest 通过 | **Reasonix config.toml 实际加载的**（`.mcp.json` + `config.toml:339` 都指向它）。无 rx-core、无 git |
| **git 版** | `AppData\Roaming\reasonix\global-workspace\mcp-servers\unified-rx` | 2796 行（含 R1 接线），有 git | 开发/测试在此，但**不是运行中的版本** |
| 空壳 | `D:\开发\unified-rx-mcp` | 仅 `.unified-rx-index/` | 历史残留 |

**后果**：
- 我此前做的 R1（rx-core 接线）在 git 版生效（113 测试全过），但**运行版还是旧代码**
- 运行版没有 `rx-core/`、没有 `_rxcore_*`——用户实际跑的是"Rust 层没接线"的版本
- 两边代码各自演化（daemon.py、shadow_core.py 等 hash 均不同）→ "改了什么不生效 / 行为对不上"

**结论：副本漂移是"运行方式不受控"的最大根源，必须先统一。**

### 1.2 核心组件

```
unified-rx/
├── server.py          2679-2796 行 · MCP 协议 + 工具分发 + 日志 + 纯函数（上帝文件）
├── daemon.py          350 行 · 7 常驻循环（self/project/full/repo/shadow/window/cache）
├── *_core.py ×8       std/locate/bug(在server内)/guard/cb_index/ds/ui_check/scan_log/shadow/window
├── rx-core/           Rust crate（25 工具：math/str/sort/stat/geo/json/prime/list/fib）★R1已接线(git版)
├── lse-engine/        Rust crate（1041 行：Delta reward / UCB 树 / 跨模型经验）
├── vendor/extensions/ 4 扩展：code-analysis-enhance(13) / pr-oracle(3) / tautest(4) / stats(4)
├── scripts/           冒烟/棘轮/基准/并发检查等 10+ 脚本
├── docs/              架构分析/接口/迁移/工具清单
└── sec-workflows/     安全 CI（trufflehog/trivy/semgrep/snyk/scorecard/safety/pip-audit）
```

### 1.3 工具全景（规划基线 35 核心 + 24 扩展 = 59；2026-08-16 已演进至 **97 核心 + 24 扩展 = 121**，清单见 TOOL_INVENTORY.md）

| 域 | 工具 | 状态 |
|---|---|---|
| 文件层 | fs_read/write/stat/list | 高频 |
| 纯函数 | math_ops/text_ops/sort_search/stat_geo/json_email/prime_list/fib | 最高频（43608 次） |
| 漏洞扫描 | bug_scan/vuln_scan/bug_locate(+UCB反馈) | 高频（1383 次） |
| 防幻觉 | hallucination_guard/capability_manifest | 高频（465 次） |
| 定位/IDE | locate_edit/code_complete | 在用 |
| 工程标准 | std_check | 在用 |
| 代码库认知 | cb_index/cb_status/cb_scan | 在用 |
| 协作 | pipeline/parallel/tool_card | 在用 |
| 教训 | lesson_recall_lse/lesson_feedback/rule_feedback | 在用 |
| 扫描日志 | scan_log | 在用 |
| 扩展 cae | lsp_query/code_context/change_impact/... | **lsp_query 等 0 调用** |
| 扩展 pr-oracle | map_pr/map_local/discover_tests | **0 调用** |
| 扩展 tautest | run/doctor/init | 0 调用 |
| 扩展 stats | record/summary/status/clear | 自动打点中 |

### 1.4 运行时架构（"小总"模型）

```
RX 调用 ──▶ MCP 接口（被调用状态）
              └─ 小总（_call 分发）
                   ① 分发 _TOOLS[name]（O(1) 静态注册表）
                   ② 落盘 scan-log（_scan_log_tick）
                   ③ 耦合（影子扫描/索引关联/缓存）
                   ④ 反馈（_attach_known_issues 附已知问题）
              ├─ IDE 式引导（capability/lsp/locate/lesson/hallucination）
              └─ 动态索引（cb_*）
（daemon 自启动扫描独立于接口，唯一交叉 = 也走 _call）
```

---

## 二、问题清单（按严重度）

### 🔴 P0（必须先解决）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P0-1 | **三副本漂移**：运行版(E:)≠git版(AppData)≠空壳(D:) | config.toml 指向 E:，git 版独有 `_rxcore_*`/rx-core/，E 版无 | 改代码不生效、行为对不上、R1 白做 |
| P0-2 | rx-core 未接线（运行版） | E 版 grep `_rxcore` = 0 | Rust 层死代码（git 版已修，需同步） |

### 🟠 P1（架构级）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1-1 | server.py 2679 行上帝文件 | 实测行数 | 协议+工具+纯函数+路由+日志耦合；改一个工具动主文件 |
| P1-2 | 纯函数层内嵌 server.py（~120 行 `_m_*`） | 29 个 `def _m_*` | 与 rx-core 双实现并存，维护漂移 |
| P1-3 | 扩展加载+分发双路径 | `_call` 与 `_call_ext` 并存 | 新增扩展要改主文件；路径探测失败 24 工具静默消失 |
| P1-4 | 配置三处散落 | `.mcp.json` + `reasonix-plugin.json` + `config.toml` | 改一处漏一处；版本描述不一致（59 vs 56 vs 35） |

### 🟡 P2（质量）

| # | 问题 | 影响 |
|---|---|---|
| P2-1 | `_KNOWN_ISSUE_TOOLS` 硬编码名单 | 新增扫描工具忘加 → 反馈缺失（静默） |
| P2-2 | daemon 7 循环逻辑与 server 重复 | 扫描逻辑两处触发点 |
| P2-3 | 0 调用工具（lsp_query/code_context/change_impact/pr_oracle/tautest） | 能力存在但未接入工作流，白开发 |
| P2-4 | vendor 扩展路径硬编码 Windows（`E:\共享\51`） | 跨平台失效；CI/Linux 加载失败 |
| P2-5 | `docs/TOOL_INVENTORY.md` 说"rx-core 待合并"——已过时 | 文档与代码漂移 |

---

## 三、技术生态调研（2026-08-12 实查）

### 3.1 MCP 框架选型

| 方案 | 状态 | 适用 |
|---|---|---|
| **mcp Python SDK**（官方） | 23.9k★，活跃（08-12 还在推） | 现状就用它，稳定 |
| **FastMCP** | 27.2k★，更 Pythonic（装饰器+自动 schema） | **重写 L1 协议层时首选**——比手写 `_TOOLS` 注册表+`_schema()` 省 ~60% 代码，schema 自动生成，天然解决"注册=声明" |
| **rmcp**（Rust） | 404（仓库名待查） | 若未来全 Rust 化再评估 |
| **官方参考 servers** | 89.5k★ | 架构参考（尤其 filesystem/git 的实现） |
| **awesome-mcp-servers** | 92.1k★ | 工具灵感库 |

**结论**：不需要换通道（MCP 协议本身没问题），要换的是**实现层框架**——
FastMCP 直接解决 P1-1/P1-3（协议薄 + 注册即声明），且兼容现有 mcp SDK 生态。

### 3.2 代码智能引擎（对照 cb_* / cae_* 的定位）

| 项目 | 状态 | 可借鉴 |
|---|---|---|
| **codebase-memory-mcp**（用户已下载在 D:\开发） | 38.6k★，arXiv 论文，**C 单二进制**，tree-sitter 158 语言知识图谱 + Hybrid LSP + 15 工具，Linux 内核 3 分钟全索引，亚毫秒查询 | **cb_index 的终极形态**：知识图谱查询替代逐文件扫描；83% 答案质量 / 10× 少 token。二进制部署直接解决 Python 环境问题 |
| godot-mcp | 5.2k★，Godot 引擎桥 | 用户有多个 Godot 项目（VoxelForge 等），可作为游戏域扩展参考 |

**关键判断**：unified-rx 的 `cb_*`（AST 索引+变更感知）与 codebase-memory-mcp 功能重叠，
但后者成熟度/性能/部署形态远超手写版。**建议演进路径：cb_* 保持自研（适配 rx-core），
同时评估把 codebase-memory-mcp 作为可选高端引擎接入**（如同 pr-oracle 式的扩展）。

### 3.3 其他生态参照

- **MCP 标准**：stdio newline-delimited JSON（已在用，正确）
- **安全**：沙盒/ReDoS 防护/大小上限（已做，好实践）
- **Windows 常驻**：HKCU Run 自启（已做）+ daemon 计划任务（README 提到，需核实）

---

## 四、相关项目借鉴（用户环境内）

| 项目 | 位置 | 借鉴点 |
|---|---|---|
| **reasonix-src** | D:\开发\reasonix-src（Go，宿主） | 理解 v2 plugin manifest / 子图热重载 / doctor 诊断，让 unified-rx 部署适配更稳 |
| **Archscribe** | D:\开发\Archscribe | 架构图渲染器（已本地化），本方案配图可直接用 |
| **AI知识库_完整版** | D:\开发\AI知识库 | "炼化"方法论先例：27 项目 → 5 部分 15 章文档 |
| **mcp-tools/ocr_mcp_server.py** | D:\开发\mcp-tools | 本地 OCR MCP（RapidOCR）——之前会话 OCR 超时的备选 |
| **meow-godot-mcp** | D:\开发\meow-godot-mcp | Godot MCP 扩展参考 |
| **E:\共享\51 技能库** | anti-ai-flavor/arch-optimize/brainstorming/... | 用户技能资产；arch-optimize 与 unified-rx 分析类工具同源 |

---

## 五、目标架构（5 层，对齐已完成的 R1）

```
┌──────────────────────────────────────────────────────────┐
│ L1 协议层：mcp_server.py（薄，FastMCP 实现）                │
│    stdio / 会话 / 自动 schema / 统一分发                    │
└──────────────┬───────────────────────────────────────────┘
┌──────────────▼───────────────────────────────────────────┐
│ L2 注册层：registry.py（注册=声明，core/ext 同路径）          │
│    name → (handler, schema, group)；known_issues 自动挂载  │
└──────────────┬───────────────────────────────────────────┘
┌──────────────▼───────────────────────────────────────────┐
│ L3 实现层：tools/ 包（按域拆文件）                           │
│    scan.py  bug.py  locate.py  guard.py  cb.py  ds.py    │
│    ui.py   fs.py   lse.py   collab.py  pure.py            │
└──────┬──────────────────┬────────────────────────────────┘
┌──────▼──────┐  ┌────────▼───────────────────────────────┐
│ L4 基础设施  │  │ L5 扩展层                                │
│ scan_log    │  │  vendor/ 自动发现（跨平台路径）            │
│ scan_cache  │  │  pr-oracle/tautest/cae/stats            │
│ lse_client  │  │  + rx-core 已接线（R1 ✅）                 │
│ cb_index    │  │  + 可选: codebase-memory-mcp 引擎         │
└─────────────┘  └────────────────────────────────────────┘
```

**设计原则**：
1. 协议薄（FastMCP）→ 注册即声明 → 实现按域 → 基础设施独立 → 扩展可插拔
2. **单一来源**：副本只有一个（见 R0），配置只有一处
3. 每期可独立合并、独立验证（pytest/parity/ratchet/mcp_smoke）

---

## 六、分阶段路线图

### R0：副本统一（🚨 立即做，1 天）
- 以 **git 版（AppData）为唯一代码源**（有 git、有 R1 接线、测试全绿）
- 同步部署到运行路径：`E:\共享\51\unified-rx` ← git 版内容（或改 config.toml 指向 git 版 + 符号链接）
- 统一配置文件：.mcp.json / reasonix-plugin.json / config.toml 三处路径一致
- 验证：E 版 selftest = git 版 selftest；`_rxcore` 存在

### R1：rx-core 接线（✅ git 版已完成，待同步）
- 常驻子进程 + 白名单 + 整数规范化 + RX_CORE=0 回退
- pytest 113 全过 / parity 2310 例 0 mismatch / 1000 次调用 34ms（快 210×）

### R2：协议/实现分离（FastMCP 重写 L1+L2）
- server.py → `mcp_server.py`（薄协议层）+ `tools/` 包（按域）
- 验收：pytest 全量 + mcp_smoke；工具数 ratchet 一致

### R3：统一注册表 + 反馈自动化
- 注册=声明（schema 自动生成）；core/ext 统一分发；known_issues 由注册表元数据驱动（删硬编码名单）
- 验收：ratchet + selftest

### R4：daemon 复用 + 扩展跨平台
- daemon 通过注册表调用扫描（不再拼装）；vendor 路径环境变量化
- 验收：daemon 自测 + 非 Windows 加载测试

### R5：纯函数 Python 退役
- rx-core 全覆盖确认后删 `_m_*`（省 ~150 行）
- 验收：parity 0 mismatch + pytest 全绿

### R6（可选演进）：代码智能引擎升级
- 评估 codebase-memory-mcp 作为 cb_* 的高端替代/补充（扩展式接入）
- LSP 工具（cae_lsp_query 等）补入默认工作流（解决 0 调用）

---

## 七、验证体系（贯穿全程）

| 层 | 工具 | 现状 |
|---|---|---|
| 单元/集成 | pytest test_unified_rx.py | 113 全过（git 版） |
| Python↔Rust 一致性 | rx-core/parity_check.py | 2310 例 0 mismatch |
| 协议冒烟 | scripts/mcp_smoke.py | 在用 |
| 工具数棘轮 | scripts/tool_ratchet.py | 在用 |
| 性能基准 | scripts/bench_unified_rx.py | 在用 |
| 安全 | sec-workflows（7 个 CI） | 已配 |

---

## 八、风险与注意

1. **副本同步风险**：R0 必须用"git 版 → 运行版"单向同步，禁止反向；建议把运行路径做成部署产物（build 脚本生成），不手工编辑
2. **FastMCP 重写风险**：59 工具 schema 语义必须逐一保持（ratchet 保障）；先小范围试点（纯函数域）再全量
3. **性能红线**：用户核心诉求"内存小/速度快/高强度"——FastMCP 重写后必须复测 import 耗时/内存（现状 222ms/7MB 是基准）
4. **Windows 路径**：`E:\共享\51` 是运行版根基，重写后扩展路径必须可配置，不硬编码
5. **0 调用工具**：R6 前先确认 lsp_query/code_context 是否真的需要（避免为用而用）

---

## 九、下一步建议（按顺序）

1. **R0 副本统一**（最优先，今天就能做）——让运行版 = git 版
2. 同步 R1 接线到运行版 → 运行版 selftest + 真实调用验证
3. R2 FastMCP 分层（先纯函数域试点）
4. 每步完成后更新本文档（版本标记）

> 本方案文档与 docs/architecture-review.md 的关系：后者是"分析报告"（问题诊断），
> 本文档是"总体方案"（现状+选型+路线图），以本文档为执行主文档。
