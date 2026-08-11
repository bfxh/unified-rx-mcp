<!-- SPDX-FileCopyrightText: 2026 bfxh -->
<!-- SPDX-License-Identifier: MIT -->
# Python → Rust 迁移方案（unified-rx-mcp）

> 状态：规划中（按 CONTRIBUTING 迁移专项流程：先方案 → 评审 → 分期实施）
> 用户问题："怎么大部分都是 PY，你看看怎么改成 RUST"

## 现状（实测）

| 部分 | 语言 | 行数 | 说明 |
|---|---|---|---|
| server.py | Python | 2594 | MCP 主服务：35 核心工具 + 扩展分发 |
| core 文件（guard/std/locate/cb/ds/ui/scan_log/lse_client） | Python | 1697 | 各工具实现 |
| scripts/ | Python | ~1100 | 冒烟/棘轮/预检工具 |
| lse-engine | **Rust** | 1052 | 教训引擎（已是 Rust） |
| test_unified_rx.py | Python | ~2000 | 108 个 pytest |

**依赖**：server.py 全部**标准库**（无第三方 pip 依赖）——Rust 迁移只需
`serde_json` + 标准库，可行性高。

## 迁移收益

1. **性能**：纯函数（math/sort/stat）与扫描（bug_scan AST）预计 5-20× 提速
2. **部署**：单二进制（无 Python 环境/PATH 问题，彻底解决"本地 GOROOT/环境坏"类问题）
3. **内存**：Rust 常驻守护内存占用远低于 Python 解释器
4. **统一**：lse-engine 已是 Rust——全仓库统一语言

## 迁移风险（为什么分期）

1. **AST 静态分析**（bug_scan）从 Python ast → Rust 需用 `tree-sitter-python` 或手写解析——
   这是最大工作量（server.py 里 ~600 行 AST 逻辑）
2. **LSP 集成**（code_complete/lsp_query）——Python 走 pylsp 子进程，Rust 需 spawn
   rust-analyzer/pylsp，协议层改造
3. **扩展加载**（importlib 动态加载 cae/pr-oracle/tautest）——Rust 需 dlopen 或改进程模型
4. **测试体系**：108 个 pytest 需重写为 Rust test（`cargo test`）
5. **行为等价**：39 工具输出契约必须逐一对齐（ratchet 棘轮保障）

## 分期方案（每期独立 PR，保持可运行）

### 一期：纯函数层（低风险，收益快）
- 迁移：math_ops/text_ops/sort_search/stat_geo/json_email/prime_list/fib（约 300 行）
- 交付：`rx-core` Rust crate + cargo test 等价覆盖
- 验收：Python 版与 Rust 版 1000 次输出一致（对比测试）

### 二期：文件/路径安全层
- 迁移：fs_*（read/write/stat/list）+ 沙盒校验（_check_path）
- 交付：`rx-fs` crate（路径校验/大小限制/沙盒）
- 验收：沙盒逃逸测试（现有 pytest 场景移植）

### 三期：扫描引擎（核心价值）
- 迁移：bug_scan/bug_locate/std_check/ui_check/cb_*（AST 分析 + 正则规则）
- 交付：`rx-scan` crate（tree-sitter-python 或自研子集解析器）
- 验收：对真实项目（VoxelForge-Nexus）扫描结果与 Python 版 diff 为零

### 四期：MCP 协议层 + 守护
- 迁移：server.py 主循环（stdio JSON-RPC）+ daemon.py（常驻守护）
- 交付：`unified-rx` 单二进制（`unified-rx serve` / `unified-rx daemon`）
- 验收：mcp_smoke 协议测试 + 守护常驻测试

### 五期：扩展与收尾
- cae/pr-oracle/tautest 扩展适配（dlopen 或保留 Python 子进程桥）
- 测试全量迁移、README/CONTRIBUTING 更新、CI 增加 cargo 矩阵

## 决策点（需用户/评审确认）

1. **一期先做？** 还是先做四期（协议层）拿整体框架？
   → 建议一期（纯函数）先落地，风险最低、立即可验证
2. **扩展保持 Python 桥接？** 三期前扩展仍用 Python 子进程（渐进迁移）
3. **CI 双语言并行**：迁移期间 Python CI 保留，Rust CI 新增（matrix）

## 验收标准（每期）

- [ ] `cargo test` 全绿（新 Rust 测试等价覆盖旧 pytest 场景）
- [ ] 迁移工具输出契约与 Python 版一致（ratchet + 对比测试）
- [ ] CONTRIBUTING 流程 ④验证 ⑤审查 ⑥漏洞扫描 全过
- [ ] 独立 PR，review 无 blocking
