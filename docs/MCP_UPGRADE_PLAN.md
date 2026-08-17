# MCP 升级计划（unified-rx-mcp 能力评估 2026-08-17）

> 基于 115 个模块的完整盘点（52 能力组 + 49 测试文件）。

## 现状能力矩阵

| 领域 | 模块 | 状态 |
|---|---|---|
| 扫描 | bug_scan/std_check/ui_check/cb_scan/cov_scan/cross_taint/rust_scan/sage_scan/stress_scan + scan_all 五路 + scan_now/scan_delta | ✅ 完整（常态铁律） |
| 定位 | bug_locate/bug_bisect/git_bisect_find/failure_analyze/predict_impact | ✅ 完整（L4 授权闭环） |
| 学习引擎 | train_export / patch_learn / differentiable_code / explore_engine(LATS) / distill_pipeline / mini_bert_tokenizer / quality_engine / replay_core | ⚠️ 雏形齐备但**流水线断链** |
| IDE | ide_* (8 模块：cache/commands/fusion/permission/quest/session/ui/tools) | ✅ 完整 |
| 游戏 | game_api / game_check / geometry_tools | ✅ 已有 |
| 基础设施 | daemon/dashboard/telemetry/storage_tiers/scan_log_core/scan_trend/graph_index/search_* | ✅ 完整 |

## 升级建议（按优先级）

### P0 — 训练流水线打通（本次实施）
- **断链点**：train_export 产出 samples.jsonl，但 patch_learn/quality_engine/distill 无人消费。
- **动作**：①train_export 重跑（VoxelForge 60+ 样本）；②新增 `learn_mine_rules`：从 samples 的 bug_patterns 自动挖掘检测规则（n-gram 高频模式）→ 生成 vuln_rules.json；③`scan_all` 集成规则校验（新规则能抓历史 bug 模式）。

### P1 — 负样本自动入库
- scan 发现的问题（bug_scan/std_check 的 error 级）→ 自动 append 到 samples.jsonl（negative samples，含 diff 上下文）。
- 数据源：scan-log（已有 scan_log_core）+ feedback（bug_locate_feedback 的 UCB 奖励）。

### P2 — feedback 闭环
- bug_locate_feedback 的成功/失败 → 规则权重（vuln_rules 命中数加权）——规则越准权重越高。

### P3 — distill 蒸馏冒烟
- distill_pipeline 依赖检查（onnxruntime/torch）——可用则跑小模型蒸馏冒烟（错误分类器 ModernBERT 级）；不可用记录依赖缺口。

## 学习引擎实验（本次验证）
- 从 samples.jsonl 挖规则 → 在 VoxelForge 当前代码验证（规则应能抓历史 bug 模式或确认零遗漏）。
