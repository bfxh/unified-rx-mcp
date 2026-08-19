# CHATLOG — unified-rx-mcp 聊天记录（决策与规则来源）

> 本文件是项目"聊天记录"文档（用户规则：任何项目至少两个文档——聊天记录 + 设计文档）。
> 记录用户每次指示/决策/规则来源，供后续对话追溯"为什么这么做"。
> 追加格式：`## YYYY-MM-DD` 下按条目追加，保留用户原话关键句。

## 2026-08-19

### 1. 语义回归测试（用户：搞一个语义回归测试，每次搞完就跑）
- 用户原话："每次挖漏洞怎么就是不行啊 智商还是这么低啊 还有好多的问题啊 每次搞完就运行一下代码啊 搞一个语义回归测试"
- 决策：新增 `scripts/semantic_regression.py`——生产路径 `server._call()` 级语义锚点
  （eq/contains/json_field/json_contains/not_error/error/routeable 七种断言），
  覆盖核心纯函数族/文件层/扫描工具/协作层/扩展层 + manifest 一致性检查。
- 挂载：pre-push.sh 第 1 步、ci.yml pytest 前、README 验证节。
- 顺手修复：`_call` 扩展分发前缀缺 `ciopt_`（52 个 ciopt 工具 manifest 有但
  unknown tool——能力清单幻觉实锤）；tools.json 缺 blender_verify 漂移。

### 2. 工具导致 AI 智商降低专项（用户：找找因为工具导致智商降低的各种情况然后修复 然后演进算法）
- 用户原话："每次你要读文档啊 每干一件事就是要读文档 然后就是找找因为工具导致智商降低的各种情况 然后修复 然后演进算法分析这个MCP全部东西"
- 流程决策：**每件事先读文档**（docs/spec/设计文档）再动手——本条目后所有开发任务默认先查文档。
- 修复 8 项：mcp_smoke 断言过时（lesson_recall_lse→lesson）、guard Windows 绝对
  路径丢盘符（真实声明误判 unverifiable）、guard 行号越界误判、std_check 魔法数字
  常量定义误报+同行重复、文档工具数漂移 7 处（157/149/81/73 → 177/101）。
- 算法演进 3 条（确定性规则，error 级）：`x[len(x)]` 恒越界、负索引字面量越界
  （AST UnaryOp）、变量零分母（z=0 后 /z，重赋/参数不报）。
- 验证：pytest 163/163、语义回归 122/122、mcp_smoke PASS（177 tools）。

### 3. 用户指示默认当规则 + 项目双文档（本条规则本身）
- 用户原话："当我给你们搞某些行为，就是干某些事情，就默认把这当个小规则，把我跟你讲的东西就默认当规则就行了 好吧 如果以后有冲突那你就把那个冲突告诉我 然后就是怎么记这东西呢？就是任何项目至少两个文档，一个是聊天记录 一个是整个设计文档 然后这个规则按项目级的钩子来搞"
- 决策：
  - **一次指示即规则**（无需重复第二遍）；冲突时主动上报让用户裁决。
  - **每项目至少两个文档**：CHATLOG.md（聊天记录）+ DESIGN.md（设计文档）。
  - **规则按项目级钩子**：项目根 AGENTS.md（所有 AI 工具默认读取）固化规则 + 指向双文档。
- 落地：global REASONIX.md 元规则第 8/9 条；本仓库 AGENTS.md + CHATLOG.md + DESIGN.md。

### 4. 3D skills + 7 维缓存方案（用户：搞搞3D的skill如 Maya/Blender/GSAP/three 等等 + 考虑各种情况加 还要高质量）
- 用户原话："搞搞3D的skill如 MACskill blender skill GSAPskill three skill 等等的"、"你需要考虑各种的情况再加 还要考虑高质量"、"有些有风险的可以看看其他的地方 反正总有落脚点"
- 决策（Maya 经 ask 确认）：
  - 新建 `skills/blender-skill`（bpy/bmesh 全流程+反模式）、`skills/threejs-skill`（Three.js 网页 3D+性能）、`skills/maya-skill`（MEL/cmds+FBX 管线）；GSAP 已有官方 8 子 skill 不动；skill_templates/blender-modeling 占位符升级指向完整版
  - 7 维缓存方案逐维裁决写入 `docs/CACHE_DIMENSIONS.md`：维度 4（几何结果缓存）✅落地（load_mesh 解析缓存：mtime+size 键/深拷贝保 tuple 类型/成功才缓存/64 条 LRU）；维度 2/3/5 已有等价（speculate/cb_index+scan_cache/vuln_rules+LSE）；维度 1（语义模糊）/7（潜空间）不交付（正确性风险+无承载点，符合"有风险不交付"原则）
- 验证：pytest 182/182（含 test_load_mesh_cache_semantics）、语义回归 123/123、geometry_tools 基线 10 issues 为预存误报无新增
