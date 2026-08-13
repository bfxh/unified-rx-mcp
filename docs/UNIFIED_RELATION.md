# unified-rx 与 unified（72 工具网关）的关系

> 2026-08-13 · 对齐核查结论：**两个入口并存、分工明确，不合并**。
> unified-rx 保持主入口（57 核心 + 懒加载扩展）；unified 网关保留给"全量聚合"场景。

---

## 一、两个入口是什么

| | **unified-rx**（本仓库，主入口） | **unified**（聚合网关） |
|---|---|---|
| 位置 | `mcp-servers/unified-rx/` | `mcp-servers/unified/` |
| 工具面 | **57 核心** + 懒加载扩展（code-analysis-enhance 13 / pr-oracle 3 / stats / tautest 4） | **72 全量** = code-analysis-enhance 13 + pr-oracle 3 + ci-optimization 52 + tautest 4 |
| 特性 | 防幻觉闭环 / LSE 教训引擎 / 扫描日志 / 多智能体兼容 / IDE 全家桶 / 权限 L1-L4 | 纯聚合网关：4 子模块 importlib 加载，fail-fast 防工具名冲突 |
| 启动 | `auto_start: true`（打开 RX 即启动） | 按需手动接入 |
| 优先适配 | **RX（第一优先）** | AetherStudio 等全量场景 |

## 二、差异与决策（2026-08-13 核查）

### 1. `ciopt_*`（52 个）未并入 unified-rx——**标注不并入**
- **环境耦合**：ci-optimization 动态发现 `E:\共享\51\10\CI-Optimization\src/*.py`
  的全部顶层函数（实测 52）——依赖外部目录，跨机/跨智能体不可移植
- **功能重叠**：与 unified-rx 内建纯函数高度重叠——`math_ops`/`fib_fibonacci`/
  `prime_list`/`sort_search`/`stat_geo`/`json_email` 6 个组合工具覆盖了数学/斐波那契/
  素数/排序/搜索/统计/几何/JSON 域（ciopt 的 30 个模块大半同域）
- 独有低频面（anagram/palindrome/password_checker/matrix/datetime_utils 等）：
  需要时经 unified 网关按需调用，不常驻

### 2. `stats_*`（unified-rx 独有）不回填 unified
- stats 打点/统计是 unified-rx 的协作闭环（工具调用自动打点、scan-log 落盘），
  与 RX 工作流耦合，网关场景无意义——保持 unified-rx 独有

### 3. 扩展模块同源
- code-analysis-enhance / pr-oracle / tautest 在两个入口**共用同一份子模块**
  （unified 直接 import；unified-rx 从 `vendor/extensions/` 懒加载）——工具行为一致

## 三、选择建议

| 场景 | 用哪个 |
|---|---|
| RX 会话 / 日常开发 / 防幻觉 / IDE 增强 / 扫描 | **unified-rx**（默认，auto_start） |
| 需要 ciopt 全量纯函数（52 个）的聚合场景 | **unified** 网关 |
| 两者都装 | 无冲突（工具名前缀不同：`ciopt_*` vs 内建组合名；扩展工具同源同名——同时只连一个即可） |

## 四、维护约定

- **不把 ciopt 并入 unified-rx 核心/扩展**（环境耦合 + 重叠，见第二节决策）
- 子模块（code-analysis-enhance/pr-oracle/tautest）改动需**双入口回归**：
  unified 网关（`unified/` 目录测试）与 unified-rx 扩展（`vendor/extensions/`）都要跑绿
- 新工具命名：核心走 unified-rx 前缀体系；纯函数建议组合式（6 个组合已覆盖常用域），
  特殊纯函数走 unified 网关按需
