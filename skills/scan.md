# scan 域（bug_scan/ast_scan/std_check/ui_check/bug_locate/project_scan）
- 机制：正则 + AST-lite（Python ast 模块 + Rust/JS 掩码词法）——**非编译器语义**
- 规则分 definite（panic/unreachable/bare_except 等）与 clue（unwrap/expect/
  as_cast/indexing/bevy_*）两档；clue 全量上报是设计，不算 FP
- S16 可达性：cfg(test)/tests 目录命中降级（kind=clue/info）
- S27 修复：indexing 正则已支持 `[x.f as usize]`（此前漏报）
- 已知漏报：`[md.rotation as usize]` 类已修；数组越界的"值域可证安全"场景
  语义不可见（那是 clue 的本分）
- **code_review**（S44）：多透镜评审聚合——bug 模式 + security（硬编码
  凭据/eval/exec/os.system/shell=True/innerHTML/SQL 拼接）+ complexity
  （函数>80 行/参数>6/嵌套≥24 空格）+ TODO；mode=diff 只报 git 改动行
  （含未跟踪文件），评审补丁不再全仓扫
  边界：复杂度是行数/缩进近似，非圈复杂度；security 是模式匹配非污点分析
