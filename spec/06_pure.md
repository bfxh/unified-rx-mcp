# §6 — 纯函数一致性契约（math_ops / text_ops / 纯函数族）

## 6.1 定位（MUST）

1. 纯函数族（math/text/sort/stat/geo/conv/json/prime/list/fib）**MUST** 零依赖、
   零网络、纯计算，输入 JSON → 输出 JSON/文本，无副作用。
2. 同一工具 **MUST** 在 Python 与 Rust（rx-core）双实现下**输出语义一致**
   （probe_12 断言 parity）。

## 6.2 一致性规则（MUST）

1. **整数语义对齐**：输入全 int 时，`math_power`/`stat_median`/`geo_rect` 输出
   **MUST NOT** 带 `.0` 后缀（Rust f64 输出需 normalize——`_rxcore_normalize`）。
   - `2 ** 10` → `"1024"`（非 `"1024.0"`）
   - 奇数长度全 int 的 median → int 形式
   - `geo_rect` 全 int → int 形式
2. **除零语义**：`math_ops div(a, 0)` **MUST** 返回错误（`Error` 文本），不得抛崩溃
   （probe_13 断言）。
3. **边界**：`fib` 上限 20000、`is_prime` ≤10M、`gen_primes` ≤1M——超限 **MUST** 报错。

## 6.3 性能契约（SHOULD）

1. 纯函数调用 1000 次 **SHOULD** < 50ms（O(1) 路径打点）。
2. rx-core Rust 子进程 **MUST** 懒启动 + 崩溃自动重启；不可用时**自动回退 Python**
   （语义已对齐，回退不改变输出）。

## 6.4 扩展一致性（MUST）

1. 扩展（pr-oracle/tautest/cae/stats）**MUST** 懒加载：`_definitions()` 只读核心缓存，
   扩展构建走异步路径，禁止同步路径 `asyncio.run()`（MCP 启动必炸——已知坑）。
2. 扩展加载失败 **MUST** 仅告警跳过，不影响核心工具注册。
