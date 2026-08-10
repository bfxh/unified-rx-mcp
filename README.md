# unified-rx-mcp

**64 工具的统一 MCP（single-file, lazy-loaded, memory-lean）** —— 适配 Reasonix 扩展运行时。

## 定位

一个 MCP 入口，工具按**协作角色**分类，覆盖智能体开发全流程：

| 角色 | 工具 | 用途 |
|---|---|---|
| 🗂️ **搞仓库**（repo cognition） | `cb_index` / `cb_status` / `cb_scan` | 全库索引 + 增量变更感知 + 变更优先扫描 |
| 🧭 **引导**（guidance） | `lesson_recall` / `ds_lookup` / `ds_check` | 教训召回（防复发）/ 设计系统 token 引用与合规 |
| 🔍 **分析仓库**（analysis） | `change_impact` / `code_context` / `lsp_query` / `aether_*` | 变更影响 / 光标符号级 AST→Prompt / LSP 交互 |
| 🎯 **Qoder 式定位**（locate_edit） | `locate_edit` | 自然语言/符号 → 具体修改位置 `file:line` + snippet + AI 引导（改前取上下文/改后验影响） |
| 🐛 **挖漏洞**（bug hunting） | `bug_scan` / `bug_locate` / `ui_check` / `file_dedup_state` | 静态 bug 模式 / traceback 定位 / Bevy UI 检查 |
| 📏 **工程标准**（std_check） | `std_check` | 占位文字/命名冲突/UI硬编码/魔法数字——本地直接扫，兼容游戏/UI/前端/软件 |
| 🃏 **Tool 角色回喂** | `tool_card` | 调用任意工具 → 结构化卡片 `{role,ok,summary,detail}`（Aether AiRole::Tool 启发） |
| ⚙️ **纯函数**（math/str/json/sort/prime/stat/geo/conv/valid/list/fib） | 33 个 | 零依赖高性能计算 |
| 🔌 **扩展**（lazy-loaded） | `pr_oracle_*` (3) / `tautest_*` (4) / `cae_*` (13) | PR→测试影响 / 变异测试 / 代码分析增强 |

## Tool 角色回喂（AetherStudio PR #106/#111 启发）

AetherStudio 新增 `AiRole::Tool`：工具结果以 Tool 角色记录、**不显示为用户气泡**、
UI 渲染为**简洁工具卡片**（无角色标签）。unified-rx 在 MCP 侧提供等价能力：

- **`tool_card`**：包装任意工具调用（含扩展），返回 `{role:"tool", ok, summary, detail}` JSON——
  summary 是简洁摘要（供卡片标题），detail 是完整结果（供展开）。RX/Aether UI 据此
  渲染为工具卡片，而非大段文本气泡。
- **所有工具结果天然是 Tool 角色**：MCP `call_tool` 响应与用户消息协议分离；
  `tool_card` 把纯文本结果也规范化为卡片结构，供需要结构化回喂的 UI 消费。
- 错误（未知工具/工具异常）→ `ok:false` + 摘要，UI 可渲染失败卡片。

## 子智能体用法（Sub-Agent Dispatch）

仓库级分析（引导/搞仓库/挖漏洞类工具：`cb_*`、`std_check`、`bug_scan`、`lesson_recall`、`cae_*`）
在开启子智能体（sub-agent）时**直接分派给子智能体执行**——子智能体独立分析后只回传结论，
避免占用主上下文；在最强（max）模式下效果最佳。工具测试流程（pytest + selftest）由 CI 自带，
无需人工干预。

## 工程标准契约（std_check）

`std_check` 是**默认标准**：软件、游戏、UI 前端、文档项目通用，兼容绝大多数场景。
本地直接扫描（零网络），检查文字规范（占位/假数据/套话）、命名冲突、UI 硬编码值、魔法数字。

- **提前告知**：项目有特殊条件（如专用命名规范、非 UI 领域数字、遗留占位是故意的）时，
  调用方在提示词中**提前告知**，工具按告知执行；否则按默认标准。
- **默认调用**：无特殊条件时直接使用 `std_check`（配 `tool_card` 结构化回喂），
  无需等待人工提示——这是标准流程的一部分。
- **不臆测**：TODO/FIXME 仅统计不判违规（`summary.todo_markers`），避免误伤正常开发标记。

## 性能（用户核心诉求：内存小 / 速度快 / 高强度）

| 指标 | 值 |
|---|---|
| import 耗时 | **222ms**（懒加载 mcp 库后；重构前 2529ms，**11.4×**） |
| import 内存 | **7MB**（重构前 33MB，**4.7× 更小**） |
| 工具调用 | **3.5µs/次**（1000 次 3.5ms） |
| 工具定义 | 缓存命中 **0ms**（重构前 75ms/次） |
| 扩展加载 | 按需（调用扩展工具时才加载，保持基线最小） |

## 架构（极简）

- **单文件** `server.py`（54KB）：静态注册表 O(1) 分发，零反射
- **懒加载**：`mcp` 库只在 `run()` 协议层 import（纯工具/自检路径零依赖）
- **轻量类** `_TC` / `_ToolDef`：协议层解耦，运行时不依赖 mcp 类型
- **常驻**：`auto_start=true` —— RX 一打开自动启动，进程常驻面对大型仓库，工具调用后不消失
- **错误隔离**：单工具异常转结构化文本，绝不拖垮网关

## 安装（适配 RX）

```bash
# 方式 A：.mcp.json（当前生效）
# E:\共享\51\unified-rx\.mcp.json → install_source 安装
python E:\共享\51\unified-rx\server.py

# 方式 B：reasonix-plugin.json（v2 manifest，main-v2 升级后生效）
# apiVersion: reasonix.io/plugin/v2，支持真子图热重载/doctor 诊断
```

config.toml 条目（install_source 自动生成）：
```toml
[[plugins]]
name = "unified-rx"
command = "C:\\...\\Python311\\python.exe"   # 绝对路径（防 PATH 劫持）
args = ["E:\\共享\\51\\unified-rx\\server.py"]
auto_start = true
startup_timeout_seconds = 30
call_timeout_seconds = 300
```

## 验证

```bash
python server.py --selftest    # 64 工具自检
python -m pytest test_unified_rx.py -q   # 49 tests
```

## 安全（已审查）

- security_review pass：command 绝对路径化（防 PATH 劫持）、无 eval/exec/网络、纯 stdio
- 沙盒：`UNIFIED_RX_SANDBOX` 环境变量锚定文件工具根（未设置=不限制）
- ReDoS 防护：用户正则黑名单（嵌套量词/开区间链拒绝）
- 大小上限：文件读写 1MB / 数组 100k / 阶乘 1000 / 素数 1M

## 更新日志

- **2026-08-10** 性能重构：mcp 懒加载（import 11.4× 快 / 内存 4.7× 小）、工具定义缓存、_TC/_ToolDef 轻量类
- **2026-08-09** 安全加固：command 绝对路径、tier 无效字段清除、.mcp.json/reasonix-plugin.json 双文件部署
- **2026-08-09** 功能：bug_scan/bug_locate、ui_check、ds_lookup/ds_check、cb_index/cb_status/cb_scan

## 捐赠支持 (Donate)

如果这个项目对你有帮助，可以请我喝杯咖啡 ☕ 感谢支持！

If this project helps you, feel free to buy me a coffee ☕ Thanks for your support!

<img src="assets/donate-qr-wechat.jpg" alt="微信赞赏码 (WeChat Donate QR)" width="240" />

## 关联项目 (Related Projects)

本 MCP 的启发与生态伙伴：

- **[AetherStudio](https://github.com/aetherstudio-cn/AetherStudio)** — Windows 原生轻量代码编辑器，Tool 角色工具结果回喂（`AiRole::Tool`）与本 MCP 的 `tool_card` 结构化回喂相互印证
- **[arch-optimize](https://github.com/bfxh/arch-optimize)** — 架构优化技能（五阶段工作流 + R1-R6 风险扫描 + 质量度量），分析仓库类工具的设计参考
- **[Reasonix](https://github.com/esengine/DeepSeek-Reasonix)** — 本 MCP 适配的宿主（`.mcp.json` / `reasonix-plugin.json` 部署，auto_start 常驻）
