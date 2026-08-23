# unified-rx-v2

**本地工具代替智能体体力活的平台** —— 凡是 AI 要做的"查/做"类体力活，全部下沉为本地确定性工具；AI 只保留"决策"层。

> 定位：**工具箱，不是智能体，不是内核**。MCP 只是通道，价值在"工具 + 工作流"的完整链路。
> 七维"掌握"：**结构 / 语义 / 定位 / 探索 / 记忆 / 反馈 / 质量**。
> 设计哲学：**少而准**（~35 个组合工具，不用 183 个噪音）、**零依赖可跑**（纯 stdlib）、**写文件通道必须可靠**（fs_write 带授权直传）、**单点接开源最强**（代码语义引擎不自研）。

## 与旧版 unified-rx-mcp 的关系

| | 旧 unified-rx-mcp | unified-rx-v2 |
|---|---|---|
| 工具数 | 183（注入面 200+） | **~35 组合工具** |
| server | 7462 行上帝文件 | 协议薄层 <300 行 + tools/ 按域 |
| 依赖 | mcp SDK + 多扩展 | **纯 stdlib 零依赖** |
| 写文件 | fs_write 授权剥离（写不了） | **__authorized 直传，可靠** |
| 检索 | 5 套并存 | code_search 统一（可接开源引擎） |
| 文档 | 40+ 份方案 | README/SPEC/ROADMAP 三件套 |
| 代码智能 | 手写 AST 文本规则 | **codegraph / codebase-memory 适配器** |

旧库已备份 `D:\开发\backups\unified-rx-mcp-20260824-040352.zip`，冻结归档。

## 工具面（九域 34 工具）

| 域 | 工具 | 收敛自 |
|---|---|---|
| 📁 fs | `fs_read` `fs_write` `fs_stat` `fs_list` | 4 保留，沙盒+授权 |
| 🐛 scan | `bug_scan` `std_check` `ui_check` `bug_locate` `project_scan` | vuln_scan/scan_all/scan_now → project_scan |
| 🛠️ ide | `locate_edit` `code_context` `ide_edit_multi` `ide_rename` `ide_references` `code_complete` | 6 收敛（complete_chain/continue/jump_predict 并入） |
| 🔍 search | `code_search` `kb_query` | explore_code/semantic_search/dep_graph → code_search |
| 🛡️ guard | `hallucination_guard` `capability_manifest` | 2 保留 |
| 🧠 learn | `lesson` `chatlog_search` | lse/lesson_recall_lse → lesson |
| ⚙️ ops | `backup` `cost_report` `scan_log` | stats/telemetry/alarm → scan_log |
| 🎮 game | `game_check` `blender_verify` | game_* 全部 → game_check |
| 🧮 pure | `pure_funcs` `pure_batch` | 52 ciopt + 33 math/str → 2 |
| 🔗 collab | `pipeline` `parallel` | 2 保留 |
| 📖 meta | `cmd_cheatsheet` `local_run` | local_tools 并入 local_run |
| 🚀 engine | `engine_status` `engine_query` | 开源引擎接入（codegraph/codebase-memory） |

## 运行

```bash
python server.py            # MCP stdio 模式
python server.py --selftest # 自检
python -m pytest tests/ -q  # 全量测试
```

## 理念（本质三分）

- **设定性**：工具箱定位 / 七维掌握 / MCP 通道 / 写完即验 / 防幻觉闭环 / 本地优先
- **设计性**：工具粒度（35 组合）/ 引擎选型（接开源）/ 分层架构（薄协议+域实现）
- **疑点**：daemon 常驻扫描是否必要（默认关，按需开）/ 本地嵌入模型（≤2GB 显存约束）
