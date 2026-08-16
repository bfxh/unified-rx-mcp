# unified-rx 架构分析报告 + 重构方案

> 2026-08-12 · 基于 Archscribe 生成的当前架构图（docs/diagrams/current-arch.png）
> **历史快照**：当时 35 工具/2680 行；2026-08-16 已演进至 97 核心工具/~8000 行，工具面见 TOOL_INVENTORY.md
> **R1 已于 2026-08-12 完成**（rx-core 接线：server.py 纯函数走 Rust 常驻子进程，pytest 113 全过）

---

## 一、当前架构（6 层）

```
┌──────────────────────────────────────────────────────┐
│  RX（MCP 客户端）                                      │
└──────────────┬───────────────────────────────────────┘
               │ stdio 协议
┌──────────────▼───────────────────────────────────────┐
│  server.py（2680 行·上帝文件）                          │
│  ├─ MCP 协议处理（handle_call_tool / _call）            │
│  ├─ 35 个 _tool_* 工具实现                              │
│  ├─ 35 个 _m_* 纯函数（Python 版）                      │
│  ├─ 扩展路由 _call_ext（vendor 24 工具）                │
│  ├─ 日志打点 / known_issues 反馈                        │
│  └─ LSE 客户端 / 防幻觉                                 │
└──────┬──────────┬──────────┬──────────┬───────────────┘
       │          │          │          │
┌──────▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────────────┐
│ 8 个 core│ │ vendor │ │ rx-core│ │ daemon.py      │
│ std/loc  │ │ 24 扩展│ │ (Rust) │ │ 7 循环扫描      │
│ ate/bug  │ │ 独立MCP│ │ 纯函数 │ │ shadow/window/  │
│ guard/.. │ │ 进程   │ │ ★死代码│ │ cache/full/repo │
└──────────┘ └────────┘ └────────┘ └────────────────┘
```

## 二、架构问题（按严重度排序）

### 🔴 P0-1: rx-core（Rust）没有接线 —— 迁移做了一半 ✅ 已修复（R1）
- `rx-core/target/release/rx-core.exe` 编译存在（cargo build --release）
- **R1 修复（2026-08-12）**：
  - `main.rs` 改为行协议循环（单发 + 常驻双模式兼容）
  - `server.py` 新增接线层：`_rxcore_call`（常驻子进程 + 锁 + 崩溃重启）+ `_rxcore_wrap`（Rust 优先，失败回退 Python）
  - 工具白名单 `_RX_CORE_TOOLS`（24 个 Rust 实际支持的工具；add/sub/mul/abs 无 Rust 实现，直接 Python 零开销）
  - 整数语义规范化 `_rxcore_normalize`（math_power/stat_median/geo_rect 输入全 int 时去掉 `.0`，对齐 Python str()）
  - 环境变量 `RX_CORE=0` 整体禁用回退 Python
- 验证：pytest **113 全过**（含 perf <500ms：1000 次 Rust 调用 34ms）；parity 2310 例 0 mismatch
- 后果（修复前）：双实现并存（Python + Rust），改了 Python 忘 Rust（或反之）→ 行为漂移；Rust 层是纯死代码（白编译）

### 🟠 P1-1: server.py 2680 行上帝文件
- 协议处理 + 工具实现 + 纯函数 + 扩展路由 + 日志 + 反馈 全在一个文件
- 后果：改一个工具必须动主文件；任何一行语法错误全挂；review 难

### 🟠 P1-2: 纯函数层在 server.py 里内嵌
- `_m_*`（math/text/sort/search/stat/geo/json/email/prime/fib）约 120 行直接写在 server.py
- 与 rx-core Rust 功能完全重叠，但未删除
- 后果：同一逻辑两处维护

### 🟡 P2-1: 扩展加载 + 分发双路径
- `_call`（core 35）与 `_call_ext`（vendor 24）两条分发路径
- `_EXT_BASE_CANDIDATES` 路径探测在 server.py 里硬编码
- 后果：新增扩展要改主文件；路径探测失败时 24 工具静默消失

### 🟡 P2-2: 日志反馈（known_issues）手写名单
- `_KNOWN_ISSUE_TOOLS` 集合硬编码在 server.py
- 后果：新增扫描工具忘加名单 → 反馈缺失（静默）

### 🟡 P2-3: daemon 7 循环逻辑在 server 外重复
- daemon.py 独立进程，但扫描函数（bug_scan 等）在 server.py 里
- daemon 通过 `_run_scan_once` 重新拼装 → 扫描逻辑两处触发点

## 三、重构方案（分层解耦，一步一验证）

### 目标架构（5 层）

```
┌───────────────────────────────────────────────┐
│ L1 协议层：mcp_server.py（薄，~300 行）          │
│    stdio 协议 / 会话 / 工具路由                 │
└──────────────┬────────────────────────────────┘
┌──────────────▼────────────────────────────────┐
│ L2 注册层：registry.py（工具注册表 + 自动生成 schema）│
│    注册 = 声明：name → (handler, schema, group)  │
│    分发统一：core 与 ext 同路径                  │
└──────────────┬────────────────────────────────┘
┌──────────────▼────────────────────────────────┐
│ L3 实现层：tools/ 包（按域拆文件）                │
│    bug.py  scan.py  locate.py  guard.py        │
│    cb.py   ds.py   ui.py   fs.py   lse.py      │
│    collab.py（pipeline/parallel/tool_card）     │
└──────┬──────────────┬──────────────────────────┘
       │              │
┌──────▼──────┐ ┌─────▼──────────────────────────┐
│ L4 基础设施  │ │ L5 扩展层（vendor/ 保持独立）      │
│ scan_log    │ │  pr-oracle/tautest/cae/stats   │
│ scan_cache  │ │  （各自 server.py，注册表自动发现） │
│ lse_client  │ │                                 │
│ cb_index    │ │  + rx-core 接线：_m_* 删除        │
└─────────────┘ └─────────────────────────────────┘
```

### 重构步骤（分期，每期可独立合并）

| 期 | 内容 | 验证 |
|---|---|---|
| R1 | **rx-core 接线**：server.py 纯函数调用改走 rx-core 子进程；保留 Python 回退（开关）；parity 用例跑通 | pytest 全量 + parity |
| R2 | **协议/实现分离**：拆出 mcp_server.py（协议薄层）+ tools/ 包（按域）；server.py → 装配器 | pytest 全量 + mcp_smoke |
| R3 | **统一注册表**：工具声明元数据化（schema 自动生成）；core/ext 统一分发；删 _KNOWN_ISSUE_TOOLS 硬编码 | ratchet + selftest |
| R4 | **daemon 复用**：daemon 通过注册表调用扫描（不再拼装）；7 循环与工具同源 | daemon 自测 |
| R5 | **纯函数 Python 删除**：确认 rx-core 全覆盖后删 _m_*（省 ~150 行） | parity 0 mismatch |

### 每期验收标准
- pytest 全量通过（现有 113+）
- mcp_smoke 协议层 PASS
- ratchet 工具数一致
- 无行为回归（对比重构前后同一批调用结果）

## 四、为什么要这么重构（一句话）

**现在的问题不是"功能少"，而是"一个文件里塞了 5 个层 + Rust 白写"。**
分层后：协议薄、实现按域、扩展自动发现、纯函数单源（Rust）——改任何一层不动其他层。
