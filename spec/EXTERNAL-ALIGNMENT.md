# EXTERNAL-ALIGNMENT.md —— 外部标准对标（S142 调研轮，文档先行）

> 动机（用户指令）：「你自己看看网上 想想这个项目……你光想的 范围都太弱了」
> 「看看你那些标准、审核还有什么要记的」「不要一股脑搞堆东西」「考虑这个智能体
> 的其他情况」。方法：外部调研（2026-09-14 联网检索，来源见文末）→ 实测对标 →
> **只列精选待办（三档、不堆）** → 该记的入档（HARDENING §六 指针）。

## 一、外部坐标（四块，按对我们影响排序）

### 1. MCP 规范线已走到 `2026-07-28`——我们钉在 `2025-03-26`（落后四代）
- `2025-06-18`：移除 JSON-RPC batching；**结构化工具输出**（`outputSchema` + 
  `structuredContent`，声明即必须可校验，为兼容仍序列化进 text 块）；elicitation
  （服务器可向用户追问）；资源链接；OAuth 资源服务器化；资源类型加 `title`/`_meta`。
- `2025-11-25`：稳定线（Microsoft 指南与 OWASP MCP Top 10 以此/之后为坐标）。
- **`2026-07-28`（史上最大改版，无状态化核心）**：删除 `initialize` 握手（版本与
  能力改随每请求 `_meta` 携带）、新 `server/discover` RPC、协议级会话头移除、
  `subscriptions/listen` 取代 GET 端点、**所有 result 必带 `resultType`**
  （complete/input_required）、MRTR（InputRequiredResult 取代服务端发起请求）、
  Extensions 框架（反向域名标识、可选启用）、可缓存 list 结果（`ttlMs`/`cacheScope`）、
  tools 支持 **JSON Schema 2020-12**、新错误码（HeaderMismatch / 
  UnsupportedProtocolVersion 等）、Roots/Sampling/Logging 弃用。
- 生态：Go SDK v1.7 / C# SDK v2.0 等已实现，且**向后兼容 2025-11-25 客户端**。
- **我方现状（实测 server.py）**：`PROTOCOL_VERSION = "2025-03-26"` 固定回包，
  **不协商**（不回显宿主请求版本）；无 `resultType` / `outputSchema` / `title` /
  `annotations` / `server/discover`。当前宿主（ZCode/Yan Agent）仍在用 → 兼容性
  今天不是问题；**但它是一条会到期的账**（详见 §三 待办 B1）。

### 2. 工具面 token 经济（外部已给出量化基线）
- 外部基线：**58 个工具 ≈ 55K token**（会话开始前即付）；社区实测
  「7 个 MCP server = 67.3K token（200K 窗口的 34%）」，GitHub MCP 单家 ≈18K。
- 主流解法族（均属**宿主侧或商业平台侧**能力）：**Tool Search**（`defer_loading`，
  regex/BM25 按需装载，85–95% 定义省）、**Code execution / Code Mode**
  （把工具当代码 API，中间结果不进上下文，98.7%–99.9%）、输出压缩/渐进披露。
  取舍共识：搜索有额外时延与漏检风险（依赖工具命名/描述质量）；code-mode 需要
  沙箱设施。
- **我方实测（72 工具，本轮量）**：`tools/list` 共 **32,147 字符**
  （CJK 4,216 / ASCII 27,931）≈ **11–13K token/请求**——对照外部 58→55K 的水位，
  我们的"少而准+精简描述"传统是**明显克制**的；但仍有三个可动位：①CJK 字符
  按 ~1 token/字计费（4.2K 来自中文描述——"中文进值不进键"已做，**描述语种**
  没人算过账）；②最大单件近千字符（file_scan 996 / ide_build 807）；③域级冗余
  （宿主只用 fs+scan 时仍付全量）。S140/S141 的烧量护栏/burnwatch 管的是**会话
  消耗**，这条管的是**定义摊派**，两账互补（交叉引用 ROUNDLOG S140/S141）。

### 3. 安全标准（OWASP MCP Top 10 / Microsoft MUST 规则 / OWASP GenAI CheatSheet）
- 威胁族：**工具投毒（含 rug-pull 动态改脸）**、**间接提示注入**（工具输出携带
  敌意指令→"confused deputy"）、影子 MCP server、token 透传/管理错、上下文注入、
  会话劫持。社区扫描称 ~10% 开源 MCP server 有严重漏洞。
- 关键 MUST：服务端绝不接受非本服务签发的 token、验证全部入站请求、不依赖
  会话做认证（401 必须 challenge）、capability 变更需人工批准、全调用审计留痕。
- **我方对标**：本地 stdio、**无 token/OAuth 面（相关条目 N/A 且已记为边界）**；
  审计留痕已厚（stats.jsonl / scan_log / 三道 CI 门 / Mimosa 副本复审台账）；
  **未覆盖项 = 间接注入**：`fs_read`/`bug_scan`/`lsp diagnostics` 等**返回值直接
  就是文件/外部内容**——本仓工具的输出可以携带敌意指令进宿主上下文。现状缓解：
  截断（200/64K 档）+ 结果不外发；**缺**：内容边界标记（datamarking）策略与
  skills 侧的宿主指引（详见 §三 待办 B3）。

### 4. 工具设计最佳实践（Anthropic "Writing tools for agents" + advanced tool use）
- 已对齐：合并职能工具（我们 72 件为收敛后组合件）、命名空间前缀（fs_/ide_/
  scan 域）、分页+截断+可读的截断提示（ACI 层）、错误可修复化、has_test/engine 等
  如实标注（"诚实边界"是我方强项，外部文章里仍是建议项）。
- **未对齐**：①**evals**——外部方法论明确要求"用真实多步任务测 accuracy/token/
  调用数/错误率"；我们有 bench/ 与三轮门禁，但**没有以"工具面本身"为对象的
  任务级评测**（详见待办 B2）；②**`response_format` 式简明/详细档**（让调用方控制
  冗长度）——我们有 cursor 分页与尾部截断，静默无档位；③描述里写明"**何时不要
  用它**"（部分在 skills、未进 schema 描述）。

## 二、要记的（record，随本轮入档）

1. **HARDENING §六 新缺口组「外部对标」**：指向本文档；每次 tag 前 10 分钟复核
   规范线（对齐 §一.1 的四代差）；
2. ~~**selftest 增"工具面体量"度量行**~~（**S143 已兑，落点更正为 CI 硬门**——
   门槛不散两处：`scripts/toolface_budget.py` + core.yml 步骤 + 真门测试）：
   **38,119 字符 / ≈12.7K token** 已是可对账数字（含注解自身的 +5,972）；
3. **skills/README 增"机器面向描述纪律"**：schema 描述=给模型看的 onboarding
   （何时不用/代价/单位），人读的长文档继续住 skills/*.md；
4. **本文档来源与日期固定**；规范线滚动复核，文档顶部记下一次复核点。

## 三、精选待办（**三档，不堆**）

### A 档 · 立刻可做（低成本、纯一致性）——**S143 已全部兑现**
- ~~**A1 工具注解与标题**~~（**S143 已兑**）：核对规范原文后更正——annotations
  实为 **2025-03-26**（我们钉的版本）即有字段，此前纯属漏发。现 `list_tools`
  发 `title`（`toolmeta.py` 全 72 件中文标题，与注册表双向一致由测试锁）+
  行为提示与授权三档同口径：读档 `readOnlyHint`+`idempotentHint`，写/执行档
  `readOnlyHint=false`+`destructiveHint=true`（显式写出，不押注宿主实现规范
  默认值）。
- ~~**A2 工具面体量仪表**~~（**S143 已兑**）：`scripts/toolface_budget.py` 进
  core.yml 硬门（形状锁同步）；摸底 **38,119 字符 ≈ 12.7K token**（其中注解
  脚手架 +5,972 字符——CJK 标题 359、提示键 ~5.6K），软帽 **45,000**（实测
  +18%），超帽即红、抬帽须记账；`tests/test_s143_toolface.py` 四锁（含"压帽
  到 1 必须红"的真门验证）。
- A3 本文档 + HARDENING 指针 + ROUNDLOG 条目（S142 已做）。

### B 档 · 评估后做（有决策点，不抢跑）
- **B1 协议线升级**：先探两个宿主（ZCode / Yan Agent）各自支持的规范线 →
  决定 **dual-stack 兼容（识别 `_meta` 版本、回错 `UnsupportedProtocolVersion`
  语义）还是直拆 2025-11-25/2026-07-28 子集**（无状态化对本地 stdio 影响面小；
  `resultType` 是必带字段——升级后所有 result 加一个字段即可起步）；
- ~~**B2 任务级评测（evals）**~~（**S144 已兑**）：`bench/tool_evals.py`——13 个
  确定性多步任务（写读/扫描/污点/死代码/调用图/影响面/改码跑测/覆盖率/聚类/
  依赖环/异或/符号地图），逐题记账 calls/errors/chars，基线
  `spec/tool-evals-baseline.json`（判红=任务失败或体量 >基线×1.10）；进 core.yml
  硬门；sabotage 真门验证（任务 1 必红）。首跑：13 任务 / 15 调用 / 0 错误 /
  5,879 字符。
- **B3 间接注入立场**~~（推荐 (a)+(b)；**S144 已兑**，即 (a)+(b)）~~：清单
  `toolmeta.UNTRUSTED_OUTPUT_TOOLS`（14 件内容类工具：读文件/片段/诊断）；
  协议回包前缀 `[untrusted-content …]`（`server.tool_reply`；registry.call
  嵌入式形状零变化）；`skills/workflow.md` 立"工具输出纪律"（含"疑似注入 →
  引用给用户裁决"）。**顺带实锤修复 S143 补遗**：`tools/list` 协议层只转发三
  字段——annotations 根本没上线路（registry 发了 ≠ 宿主收到）。
- **B4 描述瘦身与语种账**~~（**S144 已兑**）~~：13 条长描述 −44%（1,968 → 1,108
  字符），工具面 38,119 → **37,259** 字符。语种账实测：description 面 2,874
  est token（CJK 2,219/2,618 ASCII）；schema 面 21,384 字符（CJK 1,997）——
  **结论：机器面向保持中文**（等价信息改英文约省 ~2K token，但：①用户与 skills
  全中文；②描述若随 B1 的 defer_loading 落地将不再常驻；③语义标记比省 token
  贵）。瘦身纪律入册：**削 provenance/实现细节（轮次号、源码路径、基准数字——
  那些住 skills 与 ROUNDLOG），保语义/判据/代价/when-not**。

### C 档 · 记录接受（不追）
- Code Mode / Tool Search 属**宿主侧机制**（`defer_loading` 由宿主实现）；
  我们作为本地 stdio server 能做的是**精简描述 + 域级 profile（可选）**——
  若未来宿主支持 defer_loading，域边界已清晰（13 域）可直接映射；
- OAuth/远程化条目（资源服务器、RFC 8707 等）：**本地 stdio 无此面，N/A 记档**；
- Linux 移植（exe 名/TEMP 假设）与并发多会话：现状记录在 PANORAMA/README，
  非本轮范围。

## 四、智能体场景矩阵（"其他情况"）

| 场景 | 现状 | 顾虑 | 处置 |
|---|---|---|---|
| **Yan Agent / glm-5.3-flash**（弱模型） | 全量 72 工具 + 中文 skills | 定义摊派 11–13K/请求；弱模型选错工具概率高 | A1 注解+title 帮助选择；B4 瘦身后按域 profile（如只开 fs+scan+ide） |
| ZCode（本会话） | 全量 | 同上 | 同上 |
| 多会话并发 | 每宿主一 stdio 进程；烧量护栏 S141 已做（QPM/日量/持久化） | 跨进程总量账 | S141 已解，不重复 |
| 离线/受限网络 | 全库零网络依赖（外接件全部本地探测） | 无 | 维持 |
| Windows 现状 / Linux 未验 | exe/.exe/TEMP 假设集中 | 移植债 | 记录接受（C 档） |
| 宿主协议线差异 | 固定回 2025-03-26 | 新版宿主可能拒老回包 | B1 先探宿主 |

## 五、来源（2026-09-14 检索）

- MCP 规范变更：[2025-06-18 changelog](https://modelcontextprotocol.io/specification/2025-06-18/changelog)、
  [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog.md)（无状态化
  大改：discover/resultType/MRTR/extensions）
- 工具设计：[Anthropic — Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)、
  advanced tool use（Tool Search / Programmatic Tool Calling / Tool Use Examples）
- 上下文经济：[Cloudflare Code Mode](https://blog.cloudflare.com/code-mode-mcp)、
  Anthropic code execution with MCP（150K→2K token 例）
- 安全：[OWASP GenAI — Securely Using Third-Party MCP Servers v1.0](https://genai.owasp.org/resource/cheatsheet-a-practical-guide-for-securely-using-third-party-mcp-servers-1-0/)、
  [Microsoft MCP Security Best Practices](https://github.com/microsoft/mcp-for-beginners/blob/main/02-Security/mcp-security-best-practices-2025.md)
  （含 OWASP MCP Top 10 MCP01–MCP10）
