# RX 效果评估（工具价值矩阵 2026-08-17）

> 用户原则：**不是工具多就有用**——每个工具必须回答"对 RX（Reasonix）实际工作流有什么用"。

## 价值矩阵

| 工具/能力 | 对 RX 的实际价值 | 评级 | 备注 |
|---|---|---|---|
| scan_now / scan_delta | 写完即挖（常态铁律）——增量只扫变更 | 🟢 高价值 | scan_delta 已实战抓出真问题 |
| scan_all 五路并发 | 全量高压扫描（239ms） | 🟢 高价值 | 收尾必跑 |
| unit_rerun（新） | **有依赖就要重跑**——变更符号→依赖者集合 | 🟢 高价值 | 本迭代新增——"每一个单元的代码都要重跑"落地 |
| git_bisect_find | 二分定位引入 bug 的提交 | 🟢 高价值 | 可回溯 |
| train_export + 规则挖掘 | 修复提交→训练样本→挖出可复用规则 | 🟢 高价值 | 已证明能抓历史 bug 模式 |
| bug_locate_feedback（UCB） | 定位奖励回灌 | 🟡 待闭环 | **P2 闭环权重**（本计划实施） |
| explore_engine（LATS） | 通用树搜索 | 🟡 待验证 | 与 bug_locate 的 UCB 关系需梳理 |
| distill_pipeline | 小模型蒸馏 | 🟡 依赖缺口 | torch/onnxruntime 未装——纯逻辑先行 |
| mini_bert_tokenizer | 本地嵌入 | 🟡 待验证 | 实际消费场景少 |
| IDE 工具（ide_tools: rename/references/complete + ide_open_at/scan_fix_flow） | **工作流核心**（扫描→定位→打开→重跑闭环） | 🟢 高价值 | 2026-08-18 升级：scan_fix_flow 闭环 + unit_rerun 精确引用 |
| dashboard/daemon/telemetry | 观测 | 🔵 低价值 | 单机场景收益有限 |

## 结论
- **拉满原则**：每次搞东西，scan 系（now/delta/all）+ unit_rerun 全开——已实现。
- **工具数量不是目标**：下一阶段重点 = 闭环（feedback→权重→规则）与蒸馏，而非新增工具。
- **定期清理**：其余冗余候选（媒体/存储辅助）每季度评估一次，无用即删。
- **IDE 升级记录（2026-08-18）**：ide_open_at（定位打开）+ scan_fix_flow（扫描→修复工作台）——IDE 从孤立能力变为工作流核心：`scan → ide_open_at(定位行) → 修复 → unit_rerun(重跑依赖者)`。
