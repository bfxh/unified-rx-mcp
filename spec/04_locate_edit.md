# §4 — 定位与补全契约（locate_edit / code_complete）

## 4.1 输入契约（MUST）

1. `locate_edit(path, query)`：`path` **MUST** 为文件或目录，`query` 为自然语言/符号描述。
2. `code_complete(path, ...)`：按光标上下文补全。

## 4.2 输出契约（MUST）

1. `locate_edit` **MUST** 返回 `{ok, query, files_scanned, candidates: [{file, line, symbol, snippet, score, reason}], hint}`。
2. `candidates` 每项 **MUST** 带 `file:line` + snippet + score + reason
   （probe_09 断言：无候选时返回 `candidates: []` 而非编造——防幻觉核心）。
3. `score` **MUST** 来自真实匹配（关键词/符号），**MUST NOT** 凭空给分。
4. `hint` **MUST** 引导后续动作（改前取上下文/改后验影响），不替代 LLM 判断。

## 4.3 定位失败语义（MUST）

1. 无匹配 **MUST** 返回空 `candidates` + `files_scanned` 计数（诚实），
   **MUST NOT** 返回近似编造的位置（probe_10 断言：对无关查询返回空）。
2. 目录扫描 **MUST** 有文件数上限，防大仓失控。

## 4.4 code_complete（SHOULD）

1. 补全 **MUST** 基于真实符号/类型上下文（LSP/tree-sitter），**MUST NOT** 模板化编造。
2. 补全失败（无 LSP）**SHOULD** 降级为 tree-sitter 只读模式，并明示降级。
