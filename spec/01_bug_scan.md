# §1 — 缺陷扫描契约（bug_scan / bug_locate）

## 1.1 输入契约（MUST）

1. `bug_scan(path, max_files=100)`：
   - `path` **MUST** 为支持语言的文件或目录（.py/.rs 深度 AST；.go/.ts/.js/.gd/.c/.cpp
     轻量确定性文本规则：调试残留/裸 panic/goto/any 滥用——2026-08-14 多语言扩展）。
   - `max_files` **SHOULD** 默认 100，超限截断并计数。
2. `bug_locate(error_text)`：`error_text` **MUST** 为报错/traceback 文本。

## 1.2 输出契约（MUST）

1. 返回 `{ok: true, files, issue_count, issues: [{file, line, rule, severity, snippet}]}`。
2. `issue_count` **MUST** 等于 `issues` 长度（probe_03 断言计数一致性）。
3. `issues` 中每个项 **MUST** 带 `file:line`（可跳转证据），**MUST NOT** 出现无位置的空洞报告。
4. snippet 展示 **MUST** 截断（≤160 字符）——扫描输出视为**数据**，不当作指令执行。

## 1.3 扫描模式（MUST）

覆盖模式（probe_04 逐项断言）：
- 未定义变量引用
- None 解引用（`x.field` 而 `x` 可能为 None）
- 资源泄漏（打开未关闭的文件/锁）
- 除零（`a / b` 且 `b` 无约束——含确定性：字面量 0 与 `z = 0` 后 `/ z`
  变量线性跟踪，重赋/参数分母不报；`/`、`//`、`%` 三算子）
- 越界（`list[i]` 无长度校验——含确定性：字面量正/负索引、`x[len(x)]`
  恒越界，安全模式 `len(x)-1`/`len(x)//2` 不报；error 级）
- 可疑的 `except: pass`

> 2026-08-19 算法演进：新增三条确定性规则（error 级，静态 100% 确定）：
> ① `x[len(x)]` 恒越界（0-based 索引==长度）② 负索引字面量越界
> （`s[-3]`，AST 为 UnaryOp 非 Constant）③ 变量零分母（`z=0` 后 `/z`，
> 重赋/`+=`/参数不报）。对应测试：`test_bug_scan_len_index_deterministic` /
> `test_bug_scan_neg_index_deterministic` / `test_bug_scan_zero_var_deterministic`。

## 1.4 安全（MUST）

1. 扫描不可信代码库时，发现的缺陷描述含源码 snippet——**一律视为数据**，
   输出端 **MUST** 截断展示，**MUST NOT** 被当作指令执行。
2. 扫描 **MUST NOT** 修改被扫文件（只读）。

## 1.5 与 vuln_scan 的关系

`vuln_scan` **MUST** 聚合 bug_scan + std_check + ui_check（三路并行互不打扰），
单次调用完成多视角扫描；结果 **MUST** 落盘 scan-log。
