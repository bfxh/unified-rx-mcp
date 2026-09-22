# MCP × 模型适配审计（2026-09-22）

**VERDICT**：unified-rx 的**服务端工程**（错误语义、截断、注入前缀、门禁）成熟；**面向模型的接口层（wire 面）停在"文本块时代"**，对弱模型是**错误放大器**。差距集中在 5 处，可用 `structuredContent` + 模型档旋钮 + 3 道自检门补上。

## 0. 谁在消费它（适配的对象）

| 消费方 | 特征 | 对 MCP 的要求 |
|---|---|---|
| **本机主模型**（deepseek-v4.1-flash，即我） | 长上下文、能读 JSON 但**按 token 付费**；会写长报告 ⇒ 大结果在上下文里二次增长 | 结果**结构化 + 可溢出到文件**；宁要短而准 |
| **弱模型 / 其它 agent**（"垃圾智能体"，需兼容） | 窗口小、工具选择易错、不信 schema、把散文当数据 | 工具少、参数有锚（enum/必填）、**错误必须是结构化且可行动**、结果必须能塞进窗口 |
| **非模型客户端**（脚本/CI 直调 `call()`） | 只看 `ok` 一个字段 | 已有（S7/S61 统一）✅ |

**关键**：契约必须**与模型能力无关**——强模型能自我补偿，弱模型不能；只按强模型设计 = 把错误外包给下游。

## 1. 实测证据（不猜）

| # | 事实 | 来源 |
|---|---|---|
| E1 | 80 工具 / 14 域；工具面 34.8KB（收窄后 `core`=54 件） | `plugin_cost.py` / 本会话工具表 |
| E2 | ~~必填 0 / enum 0~~ **【已更正，见 §1.1】** 实为：21 件无必填、**59 件声明了 `required`**（1–4 个）；251 个参数中 **14 个有 `enum`**；**11 个参数缺 `description`** | 重解析 `body.tools[].input_schema` |
| E3 | 命名拥挤：`ide×20`、`scan×6`、`code×6`、`sys×6`、`fs×4`（弱模型的选择混淆面） | 同上 |
| E4 | wire 成功 = **JSON 文本块**（`json.dumps(result)` 塞进 `{"type":"text"}`）；失败 = **散文** `ERROR: …`（+`DETAIL:`）；`isError` 正确 | `server.py:tool_reply` |
| E5 | **未使用 MCP `structuredContent`**（grep 全仓为空） | `grep structuredContent` |
| E6 | 截断 = **绝对值** `MAX_STR_CHARS=64KB`（头 48KB+尾 16KB）、嵌套深度 3、带 `<k>_total_items` 兄弟标记 | `registry.py:21,355,369` |
| E7 | **无按模型窗口的预算旋钮**（无 context-budget / max_result_bytes） | grep 为空 |
| E8 | 注入防护：非可信来源文本加 `[untrusted-content …]` **前缀** | `server.py:97` |
| E9 | 工具内部 `return {"error": str}` 会被 registry 转 `ok:false`（**这个做对了**，不冤枉） | `registry.py:525` |

## 1.1 测量更正（2026-09-22，**重要教训**）

首版审计里「必填 0 / enum 0」是**测量假象**：wire 上每条工具的 schema 字段名是
**`input_schema`**（snake_case），而我读的是 `parameters` / `inputSchema` ⇒ 取不到就静默
得 0，于是得出了"弱模型毫无锚点"的错误结论，并写进了 F4 的严重度。

重测（同源同文件）真实值：**21 件无必填、59 件有 `required`（1–4 个）；251 个参数中 14 个带
`enum`**。⇒ 本仓的参数锚**基本齐备**，真缺口只有 11 个参数缺 `description`（已补）。

教训（对"用日志做审计"这条路线同样成立）：**取不到值 ≠ 值为 0**。凡是"读到空就当空"的测量，
必须补一条**正向对照**（例：内置工具 41 个中 31 个有 `required`，说明解析路径本身是通的；
若连内置都读到 0，就该先怀疑解析而不是先下结论）。

## 2. 适配缺口（每条附"错误放大链"）

**F1 ⚠️·P0 — 单一形态缺失：成功 JSON / 失败散文**
弱模型读到 `ERROR: xxx`（纯文本）时：① 不查 `isError` 就当内容是数据；② 或对成功/失败套同一套解析，两边都错。⇒ 错误直接进模型结论。
**修**：失败也回 JSON（`{"ok":false,"error":{"kind","hint","where"}}`），`isError:true` 不变；人类可读性放 `error.human` 字段。**一个形态，模型只学一次。**

**F2 ⚠️·P0 — 无 `structuredContent`（E5）**
每条结果都要"序列化→读文本→再解析"一趟。强模型付 token，弱模型直接解析失败（长 JSON 在文本块里被截半 ⇒ 幻觉补全）。⇒ 幻觉 = 最贵的错误。
**修**：`tools/call` 同时发射 `structuredContent`（+ 每工具 `outputSchema`）；text 块保留给旧客户端（向后兼容）。

**F3 ⚠️·P0 — 大结果"截断"而不是"溢出到文件"（E6/E7）**
截断 = 静默丢信息（弱模型不知道丢了什么，照结论走）；且大结果**永久留在上下文里**按剩余轮数重复计费（实测本会话 94 轮放大 87.5×、日烧 ≈160MB）。
**修**：超阈值 ⇒ 落盘到沙盒内（如 `<sandbox>/.urx-spill/<tool>-<hash>.json`）+ 回包给「摘要 + 路径 + 取用命令」；截断仅作最后手段。**这条同时治本仓最大的成本项**（见 `CONCURRENCY-PROTOCOL.md §8.1`）。

**F4 ⚠️·P1 — 参数锚【已按更正后的实测收窄】**
原判据（"0 必填 / 0 枚举"）基于**错误测量**（见 §1.1）。真实的缺口只有一项：**11 个参数缺
`description`**（弱模型对没说明的参数只能猜）。**已补**（`fs_stat.path`、`std_check.path`、
`ui_check.{path,max_files}`、`ide_rename.{root,symbol,new_name}`、`engine_query.query`、
`big_input.{tool_name,base_args,fuzz_field}`）⇒ 现缺口 **0**。
**剩余可选**：抽查 `enum` 是否覆盖各参数的真值域（现 14 个参数有 enum；`code_review.mode`、
`ide_build.action`、`code_semantic.mode`、`ide_vscode.action`、`file_scan.engine` 等已具备）。

**F5 ⚠️·P1 — 选择面拥挤 + 无路由（E3）**
`ide×20` 等前缀群：弱模型在近义工具间随机选（`ide_lsp` ↔ `ide_impact` ↔ `code_search`），选错=答错且**看起来像答对**。
**修**：① 工具名带"意图动词"（`find_*` / `check_*` / `read_*`）；② 提供**路由入口** `which_tool(intent)`（扩展 `capability_manifest`），弱模型先问再调。

**F6 ⚠️·P1 — 注入防护用前缀（E8）**
前缀在模型**摘抄/总结**时会丢（它复制正文，不会复制前缀）；丢前缀 = 丢信任标记。
**修**：信任作为**字段**（`trust:"untrusted"`、`source:"<path>"`）随 `structuredContent` 一起走；前缀保留为冗余提示。

**F7 ⚠️·P2 — 适配无自检门**
"能不能被弱模型用"目前**不可测**（没有一条门会因"错误形状退化"而红）。
**修（3 道门，都可机器判）**：
- **G1 弱模型模拟器门**：以弱模型行为调用（空参 / 错键 / 大小写错 / 超长值 / 串错工具名 / 缺 `__authorized`）⇒ 断言每个都得到**结构化、带 next-step 的错误**且**绝不返回成功形状**；
- **G2 回包预算门**：逐工具最坏输出 P95 ≤ 预算；超限必带「分页 or 溢出路径」标记；
- **G3 消息形状门**：wire 层禁散文错误（断言不存在 `ERROR:` 前缀形态，只有 JSON）。

## 3. "很乱"在哪（三层，分层处置）

| 层 | 现状（数字） | 处置 |
|---|---|---|
| **L1 MCP 面** | 80 工具 / 14 域 / `ide` 前缀 20 件；加一个工具要同步 README×3 + PANORAMA + toolmeta + 多个计数测试（**18 处**） | P1 的 F5 路由 + 归并；计数连锁由已有门兜住，**不再新增需同步的面** |
| **L2 文档面** | `spec/`+`docs/` ≈ 20 份 md + README 三语；同一事实多处陈述 | 入口收敛：`docs/INDEX.md` 只列"活文档"，归档移 `docs/archive/` |
| **L3 工作区面** | `default/` 根 **178 项**，其中 **56 个一次性脚本/草稿**（`patch_*.py`、`mx_*.sh`、`commit-msg-*.md`、`_*.ps1`）——**这是我自己造成的** | 只留活产物（`NOTATION.md`/`symbols.json`/`symbol_check.py`/`token_audit.py`/`cap.sh`/`perf_lock.py`/`wt.sh`/`plugin_diet.py`）；其余移 `_attic/`、`commit-msgs/`。**未执行**：其它会话可能引用（等你点头或会话全停时做） |

## 4. 落地顺序（建议）

```
P0（契约，改一次全体受益）：F1 统一失败形态 → F2 structuredContent → F3 spill
P1（弱模型档）：F4 enum/required/示例 → F5 路由 → F6 trust 字段
P2（把适配变可测）：G1 弱模型模拟器门 → G2 预算门 → G3 形状门
```
每步都配一条 G（否则"改好了"不可证）。P0 三条**不改语义**、只改 wire 形状 ⇒ 风险低、可在 S 轮内完成。

## 6. 落地记录（S161，2026-09-22）

**P0 三件已落地**（wire 层，`registry.call` 形状零变化——沿用 S144 注入前缀的同款设计）：

| 件 | 契约（现行为） | 位置 |
|---|---|---|
| **F1 一形态** | 失败回包**也是 JSON**：`{"ok":false,"error":{"message","detail","next"}}`；散文 `ERROR: …` 废止 | `server.py::tool_reply` |
| **F2 structuredContent** | 机器可读层**统一包络**：成功 `{ok:true,data:…}` / 失败 `{ok:false,error:…}`（+ 不可信工具带 `trust`/`source` 字段） | 同上 |
| **F3 溢出落盘** | 超阈值（`UNIFIED_RX_SPILL_KB`，默认 48KB）⇒ 写 `<沙盒根>/.urx-spill/` 并回「摘要+路径+取用命令」；**截断降为最后手段**（落盘无上限，信息不丢） | `tools/spill.py` + `server.py` |

**有意保留的兼容**：文本块里**成功**仍是结果原文（旧消费方零改动；`mcp_surface_gate` 的
`sys_topology.vendor` 断言因此原样通过）；只有**失败**从散文变 JSON——那不兼容面本来就是坏的。

**为什么 spill 单列模块**：手写路径逻辑被安全扫描连拦三次（join+realpath / `_resolve` /
显式三步校验都被判"路径穿越"）。最终形态是**结构上消除该面**：文件名由
`tempfile.mkstemp` 生成（不由我们拼），目录走三步校验（abspath+normpath → 禁 `..` → 必须
在沙盒根内），前缀先过 `[A-Za-z0-9_]` 白名单。归档：`Path` 拼接不再出现在协议层。

**新增门**：① `mcp_surface_gate` 15 → **21 条契约**（+6：缺参 isError、错误是 JSON、
错误带 `next`、失败 structuredContent、成功 structuredContent、容器面同形）；
② **`scripts/model_fit_gate.py`（新，S161）**——对真实 stdio server 施压：
**G1 弱模型模拟器**（缺参 / 空值 / 沙盒外路径 / 未授权写 / 未知工具 / 超长参数 6 例，
每例必须"结构化 + 带 `next` + 非成功形状"）、**G2 回包预算**（代表工具回包 ≤64KB
或必须溢出落盘且给 `path`+`fetch`；溢出与截断不得并存）、**G3 一种形态**（所有文本块
可解析 JSON）。**首跑即抓到我自己写的解析 bug**（前缀只剥标记没剥整行）——门有牙。
③ `tests/test_s161_model_fit.py` 5 项回归锁（含"恶意前缀不穿越""落盘的是**完整**结果"）。
**计数连锁已同步**：本地门 15 → **16 步**（速档 12 → **13 步**）、README 门清单 30 → **32 项**、
`skills/workflow.md` 与 `spec/HARDENING.md` 步数、`tests/test_s145_gates.py` 的 STEPS 名单。

**未做（P1/P2，留在上面章节）**：F5 已完成（见下）、G1/G2 已完成（见下）。
剩余：**F4 的可选部分**（抽查 enum 覆盖真值域）、**F6 在 fs 工具上的显式示例**。

**F4 切片已落地**：11 个缺失 `description` 的参数补齐（现缺口 0）；F5 落地为
**`capability_manifest(intent=…)` 路由**——零新增工具面，弱模型"先问再调"。

**F5 的关键教训（实测）**：路由首版用纯文本模糊分，把「找出哪些地方调用了这个函数」
路由到 `ide_break`、「有哪些引用」路由到 `ide_dead_code`——**因为"函数/引用"这类词在很多
工具描述里都出现**。改为 **curated 意图簇为主判据**（32 簇，含中文常见变形，例
"没用到/没被用到/没人用"），模糊分只兜底且排在人写候选之后；实测 7/7 命中
（`调用/引用→ide_callgraph`、`评审→code_review`、`编译→ide_build`、`重命名→ide_rename`…），
回包带「参数/必填/写操作」供弱模型照抄，体积 <4KB。不带 `intent` 时**旧形状原样保留**
（既有消费方不受影响）。


- 我（v4.1-flash）能读 JSON 文本块，**但那是付费的**：F2+F3 直接砍我的上下文成本（本会话实测放大 87.5×）。
- 我会写长报告 ⇒ **spill 设计比"求 agent 自律"可靠**（自律不可验证，落盘可验证）。
- 强模型能补偿的（F1 两形态、F4 无锚），弱模型不能 ⇒ **契约按最弱消费方设计**，强模型只是"额外受益"。
- 本仓已做对且不该动的：错误语义统一（S7/S61）、注入前缀、截断保头尾、兄弟标记、渐进披露 profile、协议面门。**审计结论不是重写，是补 wire 层三件 + 三道门。**
