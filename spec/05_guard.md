# §5 — 防幻觉契约（hallucination_guard / capability_manifest）

## 5.1 定位（MUST）

1. `hallucination_guard` **MUST** 对 AI 声明文本做事实核查：提取 `file:line`、反引号符号、
   工具名等声明，对照本地证据输出三分级：
   - `verified`：有本地证据（文件存在/行号在范围/符号在文件内/工具在注册表）
   - `refuted`：**被证伪（幻觉）**——必须纠正后才能继续
   - `unverifiable`：本地无法验证——不得当作事实传播，先取证再引用
2. `capability_manifest` **MUST** 输出全部工具 + "有什么/没有什么"边界声明
   （防能力幻觉——AI 不得声称自己具备不存在的工具）。

## 5.2 输出契约（MUST）

1. `hallucination_guard(text, root?)` **MUST** 返回结构化三分级结果
   （probe_11 断言：对存在文件返回 verified、对不存在路径返回 refuted/unverifiable）。
2. 分级 **MUST** 基于真实文件系统/注册表检查，**MUST NOT** 猜测。

## 5.3 闭环（SHOULD）

1. `refuted` 结果 **SHOULD** 自动回灌 LSE 教训引擎（负 delta 惩罚该幻觉模式），
   lse-engine 未构建时降级为"检测但不回灌"。
2. 回灌 **MUST NOT** 影响幻觉检测本身（降级只影响学习，不影响判定）。

## 5.4 与 session 要求的对接（MUST）

1. session 要求"引用 file:line/符号前必须验证"——本契约 **MUST** 作为
   unified-rx 工具集对消费方（AI）的强制约定，写进 README 防幻觉机制章节。
