# unified-rx 工具契约（Tool Contract）

> 本目录定义 unified-rx **工具集**的行为契约（RFC 2119 规范性规则）。
> 定位：**这是工具集，不是智能体**——契约约束的是每个工具的行为边界
> （输入/输出/失败语义/安全约束），不是"AI 该怎么做事"。
> 每条规则对应 `probes/` 下的可重放验证脚本；`reports/` 汇总探针实测结果。

## 目录

| 章节 | 覆盖工具族 | 契约要点 |
|---|---|---|
| [00_overview](00_overview.md) | 全部 | 工具集定位、防幻觉、沙盒、失败语义 |
| [01_bug_scan](01_bug_scan.md) | `bug_scan` / `bug_locate` | 静态缺陷扫描契约 |
| [02_std_check](02_std_check.md) | `std_check` / `ui_check` | 工程标准检查契约 |
| [03_cb_scan](03_cb_scan.md) | `cb_index` / `cb_scan` / `cb_status` | 代码库认知契约 |
| [04_locate_edit](04_locate_edit.md) | `locate_edit` / `code_complete` | 定位与补全契约 |
| [05_guard](05_guard.md) | `hallucination_guard` / `capability_manifest` | 防幻觉契约 |
| [06_pure](06_pure.md) | `math_ops` / `text_ops` / 纯函数族 | 纯函数一致性契约（Python/Rust 双实现 parity） |

## 规范性词（RFC 2119）

- **MUST**：必须满足，违反即缺陷（probe 断言）
- **SHOULD**：应满足，例外需明确理由
- **MAY**：可选，不承诺

## 探针与报告

- `probes/probe_XX_*.py` —— 每条契约的可重放验证脚本，退出码 0=通过
- `reports/REPORT_*.md` —— 探针实测汇总（findings 表，标注 verified/refuted/unverified）

## 一条契约的完整生命周期

1. 需求/缺陷 → 写进对应 spec 章节（MUST/SHOULD）
2. 写 probe 验证当前实现是否符合契约
3. 跑 probe → 结果记入 reports/（verified 或 refuted）
4. refuted → 修实现 → 重跑 probe → 转 verified
