# unified-rx-v2 工具契约（SPEC）

> 定位：**工具箱，不是智能体**——工具产出证据与事实，不替代 LLM 推理。
> 目标：把智能体的"查/做"类体力活全部本地化、确定性化；AI 只保留"决策"。

## 工具面（12 域 34 工具）

| 域 | 工具 | 契约要点 |
|---|---|---|
| fs | `fs_read` `fs_write` `fs_stat` `fs_list` | 沙盒（UNIFIED_RX_SANDBOX，分号分隔）；读 ≤1MB；**fs_write 必须 `__authorized: true`**（无授权 PermissionError） |
| scan | `bug_scan` `std_check` `ui_check` `bug_locate` `project_scan` | bug_scan 多语言静态模式；std_check 占位/魔法数字；ui_check Bevy 死按钮；project_scan 三路组合 |
| ide | `locate_edit` `code_context` `ide_edit_multi` `ide_rename` `ide_references` `code_complete` | **ide_edit_multi 内容匹配**（非行号，修复 0 应用）；rename 只建议不落盘 |
| search | `code_search` `kb_query` | BM25 符号加权（中英通用）；零嵌入模型依赖 |
| guard | `hallucination_guard` `capability_manifest` | 声明三分级 verified/refuted/unverifiable；能力边界清单 |
| learn | `lesson` `chatlog_search` | 教训 JSONL 库；中英 2-gram 匹配召回 |
| ops | `backup` `cost_report` `scan_log` | zip 快照限量 7 份；统计自动打点；扫描日志 JSONL |
| game | `game_check` `blender_verify` | 游戏规则检查；Blender 窗口实地验证 |
| pure | `pure_funcs` `pure_batch` | ~40 动作（math/str/json/sort/prime/stat/geo）；批量执行器 |
| collab | `pipeline` `parallel` | 步骤链（preset 配方）；≤8 并发 |
| meta | `cmd_cheatsheet` `local_run` | 白名单命令；**执行需 `__authorized:true`**；subprocess+超时+PYTHONUTF8 |
| engine | `engine_status` `engine_query` | 开源引擎探测（codegraph/codebase-memory）；不可用降级 BM25 |

## 安全契约（MUST）

1. **写操作授权**：fs_write 必须 `__authorized: true`，否则 PermissionError——防 AI 幻觉乱写
2. **沙盒 fail-closed**：`UNIFIED_RX_SANDBOX` 锚定文件工具根；**未设置 = 一律拒绝**；`*` = 显式放开（仅自检）
3. **大小上限**：读/写 1MB，fs_list ≤200 项
4. **命令白名单**：local_run 仅 `_COMMANDS` 内模板；参数安全字符校验；**执行必须 `__authorized is True`**（1/"true" 等伪造形态一律拒绝）
5. **错误隔离**：单工具异常 → `{ok:false, error}`，不拖垮协议层

## 失败语义

- 工具返回 `{"error": ...}` → 业务失败（ok:false）
- 抛异常（PermissionError/ValueError）→ registry 捕获 → `{ok:false, error: "TypeError: ..."}`
- MCP 层：失败 → `isError: true` + ERROR 前缀文本

## 分工判据（智能体活 vs 工具活）

| 智能体干 | 工具箱干 |
|---|---|
| 判断、决策、意图理解、写代码本身、review | 规则可枚举的确定性体力活 |
| 定位 bug 的"为什么" | bug_scan 的"哪里命中了已知模式" |
| 从模糊描述找到该改哪 | fs/搜索/批量/备份/测试收输出 |

一条测试：**交给实习生照文档做不会有歧义 → 是工具活；需要看上下文做判断 → 留给智能体。**

## 收录与改动门槛（新工具/add 功能必过）

1. 确定性可复现：同样输入同样输出，禁止依赖随机/环境巧合
2. 有安全边界：文件访问走沙盒；写/执行需 `__authorized`；命令必须白名单
3. 结构化失败：错误返回 `{ok:false, error}`，不抛到协议层
4. schema 描述一句话说清"何时用"，注册进 selftest 分组
5. tests/ 至少 1 例回归测试；`python -m pytest tests/ -q` 全绿不许倒退
6. 新工具必须替代旧工具（少而准），或归入既有域

## 验证

```bash
python server.py --selftest   # 注册表自检（工具数 + schema + 抽样）
python -m pytest tests/ -q    # 全量测试（含安全模糊集 test_security_fuzz.py）
python _mcp_probe.py          # MCP 协议联通（initialize/list/call/授权）
```

评测与质量设计见 [EVAL.md](EVAL.md)（价值假设 / 三层基准 / 九维评分卡）。
