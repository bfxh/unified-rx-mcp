# unified-rx-mcp

**56 工具的统一 MCP（single-file, lazy-loaded, memory-lean）** —— 适配 Reasonix 扩展运行时。

## 定位

一个 MCP 入口，工具按**协作角色**分类，覆盖智能体开发全流程：

| 角色 | 工具 | 用途 |
|---|---|---|
| 🗂️ **搞仓库**（repo cognition） | `cb_index` / `cb_status` / `cb_scan` | 全库索引 + 增量变更感知 + 变更优先扫描 |
| 🛡️ **防幻觉**（hallucination guard） | `hallucination_guard` / `capability_manifest` | AI 声明事实核查（verified/refuted/unverifiable 三分级）+ 能力边界清单（有什么/没有什么） |
| 🧭 **引导**（guidance） | `lesson_recall` / `ds_lookup` / `ds_check` | 教训召回（防复发）/ 设计系统 token 引用与合规 |
| 🔍 **分析仓库**（analysis） | `change_impact` / `code_context` / `lsp_query` / `aether_*` | 变更影响 / 光标符号级 AST→Prompt / LSP 交互 |
| 🎯 **Qoder 式定位**（locate_edit） | `locate_edit` | 自然语言/符号 → 具体修改位置 `file:line` + snippet + AI 引导（改前取上下文/改后验影响） |
| 🐛 **挖漏洞**（bug hunting） | `bug_scan` / `bug_locate` / `ui_check` / `file_dedup_state` | 静态 bug 模式 / traceback 定位 / Bevy UI 检查 |
| 📏 **工程标准**（std_check） | `std_check` | 占位文字/命名冲突/UI硬编码/魔法数字——本地直接扫，兼容游戏/UI/前端/软件 |
| 🃏 **Tool 角色回喂** | `tool_card` | 调用任意工具 → 结构化卡片 `{role,ok,summary,detail}`（Aether AiRole::Tool 启发） |
| ⚙️ **纯函数**（math/str/json/sort/prime/stat/geo/conv/valid/list/fib） | 33 个 | 零依赖高性能计算 |
| 🔌 **扩展**（lazy-loaded） | `pr_oracle_*` (3) / `tautest_*` (4) / `cae_*` (13) | PR→测试影响 / 变异测试 / 代码分析增强 |

## 防幻觉机制（AI 事实核查，必须使用）

**Why:** AI 幻觉（编造 file:line、编造符号、冒充不存在的能力）是最大正确性风险——
引用错误代码位置、声称工具不存在/存在、数字凭空断言，都会直接污染后续所有决策。

**How to apply（三层机制，缺一不可）：**

1. **引用前先验证**：AI 在声明任何代码位置（`file:line`）、反引号符号、工具名之前，
   必须调用 `hallucination_guard` 验证。输出三分级：
   - `verified`：有本地证据（文件存在 / 行号在范围内 / 符号在文件内 / 工具在注册表）→ 可引用
   - `refuted`：**被证伪（幻觉）**——必须纠正后才能继续，不得引用错误位置/符号
   - `unverifiable`：本地无法验证——不得当作事实传播，先取证再引用
2. **开头先亮边界**：对话开始时调用一次 `capability_manifest`，明确"有什么、没有什么"。
   工具只产出**证据与事实**，不替代 LLM 推理；不能联网、不能执行任意代码、
   不能访问沙盒外路径——防止 AI 幻觉自己具备不存在的能力。
3. **诚实标注**：无法验证的数字断言（如"49 个测试"）不会自动提取冒充证据——
   需要对照时显式给出可验证项（如 `pytest --collect-only` 的实际计数）。

**防幻觉闭环（自动回灌）**：`hallucination_guard` 发现 `refuted`（被证伪=幻觉）时，
自动回灌 LSE 引擎——①负 delta 惩罚该幻觉模式（同内容教训汇聚同一 ID，形成枢纽）；
②经验教训卡片入库（`experience_store`），下次 `lesson_recall_lse`/`experience_match`
可召回防复发。lse-engine 未构建时降级为"检测但不回灌"，幻觉检测不受影响。

## 压缩学习 + 枢纽优先（Nature Communications 启发）

LSE 教训引擎借鉴《自然-通讯》「压缩学习 枢纽优先」思想：

- **枢纽优先（Hub-Priority）**：`lesson_recall_lse` 排序时，utility 为主键、
  recall_count（教训被反复验证/召回的次数）软加权——反复被证实有效的教训是"枢纽"，
  同等效用下优先召回（hub_bonus = min(recall,10)×0.015，不硬覆盖 utility）。
- **查询不污染枢纽信号**：新增 `lesson_recall` 查询命令（不触发 recall_count++），
  替代旧的 delta=0 查询方式（旧方式每次查询都 +1，枢纽信号失真）。
- **压缩学习（Compression）**：`experience_store` 经验卡片 summary 自动截断至
  200 字符（保留关键信息 + 省略号标记），防状态文件膨胀、防上下文污染。

配套：`lesson_recall_lse` / `lesson_feedback` 教训召回闭环，发现幻觉模式后可记录教训防复发。

## 常驻自扫与扫描日志（工具本地一直在运行）

unified-rx 是**常驻工具**（`.mcp.json` `auto_start: true`，打开会话即运行）——
MCP 调用只是访问它，不是启动它。运行形态：

- **启动自扫**：每次常驻启动时，后台线程对自己核心文件（server.py/guard_core/std_core/locate_core）
  跑一轮 bug_scan，"包括它自己也会扫自己"，结果自动落盘。
- **调用即记**：扫描类工具（`bug_scan`/`std_check`/`vuln_scan`/`ui_check`/`cb_scan`/`cb_index`/
  `hallucination_guard`/`locate_edit`）每次调用完成自动追加一条到
  **`~/.unified-rx/scan-log.jsonl`**（与 lse-state.json / stats.json 同目录常驻状态区）。
- **专项目查日志**：专门搞某个项目的对话框，用 `scan_log` 工具按 `root` 过滤，
  即可查看该项目历史扫描结果（root/工具名/limit 过滤，默认最近 50 条）。
- 日志上限防膨胀（2000 条截断）；写入失败静默，绝不影响工具调用。

## 工具链协作（一次调用 = 多步流程，减少调用轮次）

`pipeline` 支持**预设配方（preset）**——AI 一次调用即可跑完完整流程，
不用手工拼 steps（1 次 MCP 调用替代 4-6 次）：

```jsonc
// 仓库审计：索引 → 漏洞 → 工程标准 → 综合（4 步 1 次调用）
{"preset": "audit_repo", "path": "/repo"}
// 幻觉守卫闭环：能力清单 → 声明验证
{"preset": "guard_text", "text": "AI 的声明文本", "root": "/repo"}
// 学习闭环：教训召回 + 能力清单
{"preset": "learn", "task": "当前任务描述"}
// 改代码前：定位 + 补全上下文
{"preset": "locate_context", "path": "/repo", "query": "要改的符号"}
```

调用方顶层参数（`path`/`text`/`root`/`query`/`task`）自动注入配方步骤的 `${key}`；
显式传 `steps` 可覆盖配方。未知 preset 报错不静默。

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
python server.py --selftest    # 56 工具自检（含防幻觉守卫抽样）
python -m pytest test_unified_rx.py -q   # 72 tests
```

## 安全（已审查）

- security_review pass：command 绝对路径化（防 PATH 劫持）、无 eval/exec/网络、纯 stdio
- 沙盒：`UNIFIED_RX_SANDBOX` 环境变量锚定文件工具根（未设置=不限制）
- ReDoS 防护：用户正则黑名单（嵌套量词/开区间链拒绝）
- 大小上限：文件读写 1MB / 数组 100k / 阶乘 1000 / 素数 1M

## 更新日志

- **2026-08-11** 常驻自扫与扫描日志：scan_log_core（~/.unified-rx/scan-log.jsonl 追加落盘）+ scan_log 查询工具（按 root 过滤）+ 启动后台自扫（打开阵地即扫自己）+ 8 个扫描工具调用自动记日志
- **2026-08-11** 防幻觉闭环 + 压缩学习枢纽优先：hallucination_guard refuted 自动回灌 LSE（负 delta + 教训卡片）；lesson_recall_lse 枢纽优先排序（recall 软加权）；lse-engine 新增 lesson_recall 查询命令（不污染 recall）+ experience summary 压缩（200 字符）
- **2026-08-11** 防幻觉机制：`hallucination_guard`（声明三分级验证：file:line/符号/工具名）+ `capability_manifest`（能力边界清单：有什么/没有什么）+ 防回归全套（P0 mcp_smoke 入 CI / P1 tool_ratchet 棘轮 / P2 actionlint 硬门禁）+ stats 会话维度
- **2026-08-11** 高协作：pipeline 步骤链 + parallel 并发组（54 工具）
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
