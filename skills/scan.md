# scan 域（bug_scan/ast_scan/std_check/ui_check/bug_locate/project_scan）
- 机制：正则 + AST-lite（Python ast 模块 + Rust/JS 掩码词法）——**非编译器语义**
- 规则分 definite（panic/unreachable/bare_except 等）与 clue（unwrap/expect/
  as_cast/indexing/bevy_*）两档；clue 全量上报是设计，不算 FP
- S16 可达性：cfg(test)/tests 目录命中降级（kind=clue/info）
- S27 修复：indexing 正则已支持 `[x.f as usize]`（此前漏报）
- 已知漏报：`[md.rotation as usize]` 类已修；数组越界的"值域可证安全"场景
  语义不可见（那是 clue 的本分）
- **S82 Rust 原生化**：std_check/ui_check/bug_locate 三工具 = rx-scan.exe
  （rust/src/scan.rs，正则全手写无 regex crate），Python 侧只剩薄壳转调
  （exe 缺失报清晰错误不静默降级；bug_locate 的 error_text 超 1 万字走 stdin）；
  bug_scan/ast_scan 仍 Python（AST 面，后续轮迁）
  - std_check：占位词 12 种（含中文）+ 魔法数（6 语言门）；注释行豁免只管
    占位词，魔法数照报；\b 按 Unicode 口径（`123中` 不报）
  - ui_check：bevy/godot/unity 三引擎；bevy 死按钮 = Marker-Query 跨 system
    验证（`With<Marker>` 或 `&Marker…Interaction` 同现即救回）；godot `$`
    ≡ 冒号后空白串含换行或直达文尾；unity 无边界 `new Button(`（renew 也中）
  - bug_locate：报错文本 → file:line；三层提取（traceback File "x.py", line N
    → 文件名 → 符号 'xxx'）；已知怪癖：文件名兜底把 foo.tsx 捕获成 foo.ts
- 名额语义：max_files 只计代码文件（非代码不烧额度）；遍历顺序 = 每层文件
  先于子目录、目录内 $UpCase 排序（os.walk 契约，S80 实锤）
- **code_review**（S44）：多透镜评审聚合——bug 模式 + security（硬编码
  凭据/eval/exec/os.system/shell=True/innerHTML/SQL 拼接）+ complexity
  （函数>80 行/参数>6/嵌套≥24 空格）+ TODO；mode=diff 只报 git 改动行
  （含未跟踪文件），评审补丁不再全仓扫
  边界：复杂度是行数/缩进近似，非圈复杂度；security 是模式匹配非污点分析
