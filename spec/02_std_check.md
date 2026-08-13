# §2 — 工程标准契约（std_check / ui_check）

## 2.1 输入契约（MUST）

1. `std_check(path, max_files=200)`：`path` **MUST** 为文件或目录，支持多语言
   （Python/Rust/Go/TS/JS 等——probe_05 断言 Go 文件可扫）。
2. `ui_check(path, max_files=100)`：`path` **MUST** 为 `.rs` 文件或目录（Bevy UI）。

## 2.2 输出契约（MUST）

1. 返回 `{ok: true, issues: [{file, line, rule, severity}], summary: {scanned, files, total, critical, warning, suggestion, todo_markers, rules}}`。
2. `summary.total` **MUST** 等于 `issues` 长度（probe_06 断言）。
3. 无问题时 **MUST** 返回 `issues: []` + `total: 0`（不得编造问题，也不得漏报）。

## 2.3 检查规则（MUST，probe_07 逐项断言）

| 规则 | 触发 | 严重度 |
|---|---|---|
| 占位文字 | lorem ipsum / placeholder / 占位 / 假数据 / 示例文案 / 待补充 / your-name 等 | warning |
| TODO/FIXME 标记 | 仅统计进 `summary.todo_markers`，**不判违规**（开发中正常标记） | 统计 |
| 命名冲突 | 同名函数/变量定义 | warning |
| UI 硬编码 | 颜色/尺寸魔法数字（Bevy UI） | suggestion |
| 魔法数字 | 无命名常量的裸数字 | suggestion |
| 死代码 | pass/return None 占位、未使用 import 启发 | warning |

## 2.4 默认标准（SHOULD）

1. 默认标准 **SHOULD** 兼容绝大多数项目（游戏/UI/前端/软件通用）。
2. 设计系统 token 合规由 `ds_check` / `ds_lookup` 负责（引用 `design_tokens.json`），
   与 std_check 职责分离。
