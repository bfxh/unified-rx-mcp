# unified-rx MCP 优化方案（v1）

> 2026-08-13 · 用户八项诉求 → 功能映射 + 实现路线
> 现状：56 核心工具 + 24 扩展工具，198 测试，本地 embedding，IDE/探索/搜索/记忆维已接线
>
> **状态：M1-M6 全部落地 ✅（2026-08-13）**——bug_hunt/ide_context preset、cmd_cheatsheet+local_run、
> skill_fetch 申请制（用户批准才下载）、design_note 三分、scan_trend 趋势分析；E: 运行版已同步

---

## 〇、用户诉求 → 功能映射

| # | 用户诉求 | 功能 | 优先级 |
|---|---|---|---|
| 1 | **挖漏洞默认跑**（智能体搞项目默认挖，不用问） | pipeline preset 注入：`bug_hunt` 默认步骤链（rust_scan+bug_scan+quality→符号聚合→修复建议） | **P0** |
| 2 | **IDE 基础内建**（看文件/类型这种必须默认知道） | `ide_context` 自动注入：每次任务前自动产出"文件→符号→依赖"基线（不进上下文，按需查） | **P0** |
| 3 | **本地命令内建**（命令写多点，少消耗 token） | `cmd_cheatsheet` 命令手册（cargo/blender/git/python 常用命令写死）+ `local_run` 直接执行 | **P0** |
| 4 | **无 skill 自动下载** | `skill_fetch` 工具：任务关键词→匹配 skill→无则从仓库/网络下载 | **P1** |
| 5 | **日志/统计自我分析** | `scan_log` 已有点击流→加**趋势分析**（高频失败规则自动提权/失效规则降权） | **P1** |
| 6 | **本质三分**（设计性/设定性/疑点） | `design_notes` 工具：项目元数据 {设定性[原样], 设计性[可调], 疑点[记录]} | **P1** |
| 7 | **好工作流** | pipeline preset 组合（默认链+领域链）+ Quest 状态机串联 | **P0** |
| 8 | **源码/二进制分离** | 项目文件双态约定：源码树（git）+ 产物树（release/bin 独立目录，命令内建发布流程） | **P1** |

---

## 一、P0 实现（本轮开工）

### 1.1 bug_hunt 默认预设（挖漏洞默认跑）
```json
bug_hunt: [
  {"tool": "rust_scan",  "as": "panic"},       // 生产危险规则（Rust）
  {"tool": "bug_scan",   "as": "bugs"},        // Python bug 模式
  {"tool": "quality_scan","as": "quality"},    // 多后端质量
  {"tool": "ide_fusion", "as": "annotated"},   // 问题→符号聚合
  {"tool": "lesson_learn", "as": "lessons"}    // 教训沉淀（可选）
]
```
- **接入点**：pipeline 默认链（无 preset 指定时先跑 bug_hunt）+ Quest 状态机 diagnose 步
- **产出**：`{panic 数/按符号聚合/修复建议}`——智能体默认看到，不用问

### 1.2 ide_context 自动注入（IDE 基础内建）
- 任务开始（root 确定后）自动生成**基线上下文**：文件清单→大文件排行→符号热点→已知问题
- 存**本地上下文文件**（不进对话 token）——智能体按需 `cb_status`/`repo_wiki` 查
- 已有基建：cb_index（全库符号+哈希）/repo_wiki/repo_graph——**加一个 `ide_context` 汇总工具**

### 1.3 cmd_cheatsheet + local_run（命令内建省 token）
```yaml
# 内建命令手册（工具内置——智能体不用搜索/试错）
cargo: [build, test --workspace, clippy --workspace, run -p nexus_app, build --release]
blender: [headless 建模命令模板]
git: [status/commit/log]
python: [pytest 全量/单文件]
```
- `local_run` 工具：执行内建命令模板（参数化）+ 结果结构化返回
- **收益**：每次任务省 3-5 轮"该用什么命令"的试错

---

## 二、P1 实现（下轮）

### 2.1 skill_fetch（无 skill 自动下载）
```
输入：任务描述（如"给 12 集团做 Blender 视觉主题"）
匹配：现有 skills_list → 命中直接用
未命中：从 skill 仓库（本地/远程模板库）拉取 Blender 建模 skill
落地：skills/ 目录 + 记入 lesson（这次学的 skill 下次直接用）
```
- 安全：下载的 skill 先沙盒校验（无恶意命令）再启用

### 2.2 scan_log 趋势分析
- 已有：每个工具调用打点（_stats_tick）
- 加：**规则有效性趋势**——bug_scan 规则命中率/误报率统计 → 高频误报规则降权，高频命中规则提权
- 产出：`lesson_learn state` 自然增长（日志→教训闭环）

### 2.3 design_notes（本质三分）
```json
// 项目元数据：.unified-rx/design.json
{
  "settled":    ["七幕剧情骨架", "12 集团解锁时序"],   // 设定性——原样
  "adjustable": ["经济数值", "trigger 机制"],          // 设计性——可调
  "doubts":     ["lua 钩子边界", "敌人 AI 方案"]       // 疑点——记录，先验证再上
}
```
- 工具：`design_note add/list`——智能体动手前查"设定性"不瞎改，"疑点"先标记

### 2.4 源码/二进制分离
- **约定**：项目根 = 源码树（git）；产物 = 独立目录（release/ 或 bin/）
- 内建 `deploy` 命令模板：build release → 复制 exe → 同步资产 → 验证
- 智能体默认"改源码树 + 产物目录只发布不手改"

---

## 三、反思点与思考点（本轮要解决的问题）

| 反思 | 现状问题 | 解法 |
|---|---|---|
| **没有知识点就动手** | 用户点名：美术任务没 skill 就搞 → 乱做 | skill_fetch + 任务前知识检查（C 类知识点先补） |
| **智能体消耗 token 多** | 每次问命令/重复读文件 | cmd_cheatsheet 内建 + ide_context 基线文件 |
| **挖漏洞要人提醒** | 手动跑扫描 | bug_hunt 默认链（P0） |
| **不懂项目本质** | 分不清哪些是设定哪些可调 | design_notes 三分 |
| **MCP 只懂系统不懂游戏/领域** | 缺领域知识库 | skill 下载 + lesson 库按项目沉淀 |
| **工作流断裂** | 工具孤岛 | pipeline 默认链 + Quest 状态机（已建，接 bug_hunt） |

---

## 四、验收标准

- [ ] bug_hunt 默认跑：任何"改代码"任务自动带漏洞扫描结果
- [ ] ide_context：任务开始自动有文件/符号基线（不进 token）
- [ ] cmd_cheatsheet：cargo/blender/git 命令内建（智能体不再问"怎么编译"）
- [ ] skill_fetch：无 skill 任务自动下载/提示补 skill
- [ ] design_notes：项目三分元数据可查
- [ ] 全部新工具测试入 pytest + E: 版同步
