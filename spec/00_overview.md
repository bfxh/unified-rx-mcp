# §0 — 工具集总契约（Overview）

> 适用范围：unified-rx 全部 80 工具（56 core + 24 扩展）。
> 定位声明：**工具集，非智能体**——本 MCP 产出证据与事实，不替代 LLM 推理。

## 0.1 定位（MUST）

1. unified-rx **MUST** 定位为工具集：每个工具只做一件事，输入 JSON → 输出 JSON/文本。
2. 工具 **MUST NOT** 假装自己有智能体能力（不能联网、不能执行任意代码、
   不能访问沙盒外路径、不能替代 LLM 推理）。
3. 工具 **MUST** 在能力边界外**显式拒绝**（报错/返回空），不得编造结果。

## 0.2 沙盒（MUST）

1. 文件读写工具（`fs_*`、`bug_scan`、`std_check`、`cb_*` 等）**MUST** 执行路径校验：
   - 拒绝空路径 / 含 NUL / 逃逸（`..` 解析后越界）
   - 默认沙盒根 = 进程启动 cwd + `UNIFIED_RX_SANDBOX` 环境变量（分号分隔）+ `D:\开发`
   - 沙盒外路径 **MUST** 抛 `路径越界（沙盒外）` 错误（probe_01 断言）
2. 沙盒可用 `UNIFIED_RX_SANDBOX=""` 显式禁用（仅用于开发调试，**SHOULD NOT** 生产使用）。

## 0.3 失败语义（MUST）

1. 工具调用失败 **MUST** 返回结构化错误（`{ok: false, error: "..."}` 或异常文本），
   **MUST NOT** 静默返回空/假成功。
2. 扩展（pr-oracle/tautest/cae/stats）加载失败 **MUST** 降级为"工具不存在"，
   **MUST NOT** 让整个 server 崩溃（双层错误隔离）。

## 0.4 防幻觉（MUST）

1. 工具 **MUST** 只返回可验证证据：file:line、符号、计数等**MUST** 来自真实扫描，
   不得猜测（详见 §5）。
2. `capability_manifest` **MUST** 在会话开头被调用一次，明确"有什么/没有什么"。

## 0.5 性能（SHOULD）

1. 纯函数调用 **MUST** 走 O(1) 路径：打点 1000 次纯函数调用 < 50ms（性能契约，见 §6）。
2. Rust 常驻子进程（rx-core）**MUST** 可用时优先；失败自动回退 Python（语义对齐）。
3. 扫描类工具 **MUST** 有文件数上限（`max_files` 默认 100-200），防失控。

## 0.6 默认挖漏洞（MUST）

1. `bug_scan` / `vuln_scan` / `ui_check` / `std_check` **MUST** 是本地静态扫描，
   零网络零费用——设计为"对话运行中/收尾自动跑"的常态工具。
2. 扫描结果 **MUST** 自动落盘 `scan-log`（`~/.unified-rx/scan-log.jsonl`），
   供后续 `scan_log` / `scan_trend` 分析（session 要求：日志/统计增强）。
