# scan 域（bug_scan/ast_scan/std_check/ui_check/bug_locate/project_scan）
- 机制：手写匹配器 + AST-lite（S83 起 bug_scan、S84 起 ast_scan 走自研迷你
  解析器 pyast.rs）——**非编译器语义**
- 规则分 definite（panic/unreachable/bare_except 等）与 clue（unwrap/expect/
  as_cast/indexing/bevy_*）两档；clue 全量上报是设计，不算 FP
- S16 可达性：cfg(test)/tests 目录命中降级（kind=clue/info）
- S27 修复：indexing 正则已支持 `[x.f as usize]`（此前漏报）
- 已知漏报：`[md.rotation as usize]` 类已修；数组越界的"值域可证安全"场景
  语义不可见（那是 clue 的本分）
- **S82 Rust 原生化**：std_check/ui_check/bug_locate 三工具 = rx-scan.exe
  （rust/src/scan.rs，正则全手写无 regex crate），Python 侧只剩薄壳转调
  （exe 缺失报清晰错误不静默降级；bug_locate 的 error_text 超 1 万字走 stdin）；
  bug_scan 随 S83 跟进、ast_scan 随 S84 跟进（均见下）
  - std_check：占位词 12 种（含中文）+ 魔法数（6 语言门）；注释行豁免只管
    占位词，魔法数照报；\b 按 Unicode 口径（`123中` 不报）
  - ui_check：bevy/godot/unity 三引擎；bevy 死按钮 = Marker-Query 跨 system
    验证（`With<Marker>` 或 `&Marker…Interaction` 同现即救回）；godot `$`
    ≡ 冒号后空白串含换行或直达文尾；unity 无边界 `new Button(`（renew 也中）
  - bug_locate：报错文本 → file:line；三层提取（traceback File "x.py", line N
    → 文件名 → 符号 'xxx'）；已知怪癖：文件名兜底把 foo.tsx 捕获成 foo.ts
- **S83 bug_scan 全量原生化**：rx-scan bugscan 子命令（rust/src/bug.rs 规则层 +
  rust/src/pyast.rs 手写 Python 迷你解析器——3.14 语义：缩进驱动、括号续行、
  f-string PEP 701 区域模型、match 软关键字回退、模式匹配全套，零第三方
  crate）。scan.py 四工具至此全为薄壳；_SCAN_CACHE 全域退役（短命 exe 无跨调
  缓存面，每次扫描都是新进程=天然新鲜）；bevy.py 转规则档案（运行时唯一实现
  在 bug.rs）。已知语义怪癖（与旧 Python 契约逐字节一致）：match 捕获变量
  （case [1,2,rest] 的 rest）是字符串字段非 Name 节点，其"使用"会报
  undefined_name——与旧 ast 版同款，非回归
- **S84 ast_scan 全量原生化**：rx-scan astscan 子命令（rust/src/astscan.rs 规则层，
  复用 pyast.rs——本轮为其补 col 列号、字符串值解码 CVal、f-string 区域位置三型）。
  astscan.py 524→103 行薄壳，scan 域五工具全薄壳。迁移坑（oracle 实锤）：
  panic 正则 \b 在可选点组之前（点形式 match 从 '.' 起）、bytes 只准 ASCII 约束的
  是源字符（b"\xef\xbb\xbf" 转义产出合法）、**CRLF 通用换行**（Python open("r")
  把 \r\n 读成 \n，exe 保留 \r 曾致行号 +4 全盘漂移——读入后归一，探针必须带与
  真实文件相同的行尾）。14 场景 oracle 逐字节 PASS 后才删 Python 码
- 名额语义：max_files 只计代码文件（非代码不烧额度）；遍历顺序 = 每层文件
  先于子目录、目录内 $UpCase 排序（os.walk 契约，S80 实锤）
- **code_review**（S44）：多透镜评审聚合——bug 模式 + security（硬编码
  凭据/eval/exec/os.system/shell=True/innerHTML/SQL 拼接）+ complexity
  （函数>80 行/参数>6/嵌套≥24 空格）+ TODO；mode=diff 只报 git 改动行
  （含未跟踪文件），评审补丁不再全仓扫
  边界：复杂度是行数/缩进近似，非圈复杂度；security 是模式匹配非污点分析
- **契约变化（S88）**：bug_scan/std_check/ui_check/project_scan 的 path、
  bug_locate 的 root（含默认 cwd）一律先过沙盒钳制（S73 纪律补全）——越界
  返回 `{"error": "路径越界（沙盒外）：…"}`，不再触碰文件系统；先于存在性检查
