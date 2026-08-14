---
name: "anti-ai-flavor"
description: "反AI味技能：检测和消除AI生成内容的特有低质量模式，包括死代码/占位符/假实现/套话/过度工程化/模型骄傲/指鹿为马。在代码生成后、文档撰写后、项目输出前必须调用。Invoke after any code generation, document writing, or project output to detect and eliminate AI-flavored low-quality patterns."
version: "1.0"
runAs: subagent
allowed-tools: read_file, write_file, bash
---

# 反AI味技能 (Anti-AI-Flavor)

## 定位

本技能专门对抗 AI 生成内容的"AI味"——即 AI 在生成代码、文本、视觉内容时反复出现的低质量模式。这些模式不是 bug，而是 AI 特有的"偷懒"方式，人类开发者一眼就能识别。

AI 味的本质是 **AI 用看起来完整的形式掩盖内容的空洞**：一段代码能跑但没有真实逻辑，一篇文档很长但信息量为零，一个声明说"已实现"但实际是占位符。本技能的使命是把这类伪装全部揪出来，让交付物经得起人类开发者的逐行审视。

与 arch-optimize 的区别：arch-optimize 关注"架构是否健康"（耦合、复杂度、回归），anti-ai-flavor 关注"内容是否真实"（有没有假实现、死代码、空话套话、指鹿为马）。两者互补，不重叠。arch-optimize v3.1 已集成 anti-ai-flavor（阶段四），本技能可单独调用做快速 AI 味检测。

## 调用时机

- AI 生成代码后（在交付给用户前必须检测）
- AI 撰写文档/报告后（在展示给用户前必须检测）
- 项目输出前（作为质量门禁的最后一道关卡）
- 用户提到"AI味""假""占位符""死代码""空话套话""指鹿为马"等关键词时

**铁律**：本技能的检测结果在交付前必须为"零 Blocker / 零 Critical"方可放行。任何占位符、假实现、指鹿为马都构成交付阻断。

## 检测维度

### 一、代码反AI味（Code Anti-AI）

| 模式 | 症状 | 严重性 | 修复方式 |
|------|------|--------|---------|
| DEAD-001 死代码 | 未使用的函数、变量、import、类 | Critical | 删除或标记为有意保留 |
| DEAD-002 占位符 | TODO/FIXME/pass/NotImplemented/"模拟实现" | Critical | 实现真实逻辑或告知用户做不到 |
| DEAD-003 假实现 | 内存模拟冒充真实系统、mock 冒充生产代码 | Blocker | 必须用真实实现，禁止用 mock 冒充 |
| DEAD-004 过度注释 | 注释说明显而易见的事（如 `i += 1  # i加1`） | Warning | 删除无用注释，保留有信息量的注释 |
| DEAD-005 无意义命名 | data1/temp/foo/bar/stuff/thing | Warning | 用描述性名称 |
| DEAD-006 过度工程化 | 不必要的抽象层、过度泛化、YAGNI违反 | Warning | 删除不必要的抽象 |
| DEAD-007 重复模式 | AI 常生成的重复 try-catch/if-else 结构 | Warning | 提取公共逻辑 |
| DEAD-008 虚假错误处理 | catch 了异常但不处理（空catch/只打印） | Critical | 要么处理要么传播，不要吞异常 |
| DEAD-009 幻觉API | 调用不存在的API/库函数/方法签名错误 | Blocker | 验证所有API调用真实存在 |
| DEAD-010 拼凑感 | 代码段之间风格不一致，像是拼接的 | Warning | 统一代码风格 |

### 二、文本反AI味（Text Anti-AI）

| 模式 | 症状 | 修复方式 |
|------|------|---------|
| TEXT-001 套话开头 | "在当今社会""随着技术的发展""众所周知" | 直接切入主题 |
| TEXT-002 空泛表述 | "具有重要意义""值得关注""不可或缺" | 用具体数据和事实替代 |
| TEXT-003 过度结构化 | 不必要的列表、过多的层次标题 | 按内容自然组织 |
| TEXT-004 虚假自信 | "显然""毫无疑问""必定" | 标注不确定性 |
| TEXT-005 AI常用短语 | "让我们深入探讨""值得注意的是""总而言之" | 用自然人类表达 |
| TEXT-006 信息空洞 | 段落看起来很长但实际信息量为零 | 每段必须有实质信息 |
| TEXT-007 重复赘述 | 同一个观点换三种方式说三遍 | 一个观点说一次 |
| TEXT-008 虚假引用 | 引用不存在的研究/数据/论文 | 只引用真实可验证的来源 |

### 三、行为反AI味（Behavior Anti-AI）

| 模式 | 症状 | 修复方式 |
|------|------|---------|
| BEHAV-001 模型骄傲 | "我已完美实现""这是一个出色的方案" | 如实报告，不加自我评价 |
| BEHAV-002 指鹿为马 | 识别错误但声称正确（原型说成背包） | 如实报告，不确定时标注 |
| BEHAV-003 声称存在不展示 | 说"有调研结果"但不展示 | 展示实际内容 |
| BEHAV-004 过度承诺 | 承诺做不到的事 | 只承诺能做到的 |
| BEHAV-005 假装理解 | 没理解用户意图但假装懂了 | 不懂就问 |

> 行为类模式（BEHAV-001 ~ BEHAV-005）由本技能在生成响应时自查，对应 `quality_standards.json` 中的 P003（指鹿为马）、P004（声称存在不展示）、P005（占位符冒充成品）。这些是硬性禁令，不是建议。

## 可执行脚本

本技能内置 2 个可执行脚本，将检测规则转化为可运行代码。AI 智能体可通过 CLI 调用并获取结构化 JSON 输出。

**设计原则**：Python 3.8+ 标准库零依赖、JSON 输出、幂等设计、多语言支持。

### 脚本总览

| 脚本 | 检测范围 | 支持语言/输入 | 输出格式 |
|------|---------|--------------|---------|
| `scripts/detect_code_ai.py` | DEAD-001 ~ DEAD-010 | Python/Go/C/C++/Rust/TypeScript/JavaScript | JSON / 人类可读 |
| `scripts/detect_text_ai.py` | TEXT-001 ~ TEXT-008 | Markdown/TXT/任意文本 | JSON / 人类可读 |

用法示例：

```bash
# 检测代码AI味（扫描整个项目目录）
python3 scripts/detect_code_ai.py --target <项目目录> --json

# 检测单个代码文件
python3 scripts/detect_code_ai.py --file <代码文件路径> --json

# 检测文本AI味（扫描文档文件）
python3 scripts/detect_text_ai.py --file <文档路径> --json

# 检测直接传入的文本
python3 scripts/detect_text_ai.py --text "综上所述，AI技术在当今社会具有重要意义" --json

# 将报告写入文件
python3 scripts/detect_code_ai.py --target ./src --json --output report.json
```

### 输出格式

每条发现（finding）包含统一字段：

```json
{
  "pattern_id": "DEAD-002",
  "severity": "Critical",
  "location": "src/service.py:42",
  "symptom": "占位符: 函数体为 pass，附带 TODO 标记",
  "remedy": "实现真实逻辑，或明确告知用户当前无法实现"
}
```

汇总报告包含 `ai_flavor_score`（0-100，越低越好，0=无AI味，100=全是AI味）。评分按严重性加权：Blocker=25 分/处，Critical=15 分/处，Warning=5 分/处，封顶 100。

### 智能体调用模式

```
1. 代码交付前：detect_code_ai.py --target <生成代码目录> --json
   → 若存在 Blocker/Critical，必须修复后重新检测，直至 ai_flavor_score 中 Blocker/Critical 计数为 0

2. 文档交付前：detect_text_ai.py --file <文档> --json
   → 若存在 TEXT-001~008 任意命中，逐条修复

3. 全流程质量门禁：detect_code_ai + detect_text_ai 联合检测
   → 作为 project-launcher 工作流的最后一道关卡
```

## 详细参考文档

| 文档 | 内容 |
|------|------|
| `references/ai-patterns-catalog.md` | 18 个模式的详细描述、真实示例（坏 vs 好）、检测方法、修复指南、按语言分类的特殊模式 |

## 与其他技能的协同

| 场景 | 推荐技能组合 |
|------|-------------|
| 代码质量门禁 | anti-ai-flavor（AI味）+ arch-optimize v3.1（架构健康+AI味）|
| 项目输出验收 | project-launcher（工作流）+ anti-ai-flavor（最后一道关卡）|
| 硬性约束校验 | anti-ai-flavor + `quality_standards.json`（P000-P005 禁令）|
| PR 代码审查 | anti-ai-flavor + arch-optimize v3.1（阶段四）|
| 文档质量审查 | anti-ai-flavor（文本维度）单独使用 |
| 快速 AI 味检测 | anti-ai-flavor 单独调用（不做架构分析）|

- 与 arch-optimize v3.1 配合：arch-optimize v3.1 已在阶段四集成 anti-ai-flavor 的检测能力，本独立 skill 用于只需快速检测 AI 味、不需要完整架构分析的场景。
- 与 project-launcher 配合：作为项目输出的最后一道质量门禁，在任何交付物移交给用户前强制运行。
- 与 quality_standards.json 配合：P000-P005 禁令是 anti-ai-flavor 的硬性约束——P003（指鹿为马）对应 BEHAV-002，P004（声称存在不展示）对应 BEHAV-003，P005（占位符冒充成品）对应 DEAD-002/DEAD-003。

## 设计原则

1. **AI味是AI的原罪**：AI生成的所有内容都默认有AI味嫌疑，必须检测，不检测不放行。
2. **假实现零容忍**：占位符/死代码/假模拟 = 欺骗用户，对应 P005 禁令，构成 Blocker。
3. **检测先于修复**：先完整检测所有AI味，再批量修复，不要边检边改导致遗漏。
4. **人类视角**：以"人类开发者看到这段代码/文本会怎么想"为判断标准——如果他一眼能看出是AI糊弄的，就是AI味。
5. **诚实优先**：宁可承认做不到，不用占位符冒充。做不到就明确告知用户当前能力边界。
6. **量化优于直觉**：ai_flavor_score 提供客观基线，Blocker/Critical 计数为 0 才可交付。
