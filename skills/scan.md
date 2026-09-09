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
- **vuln_knowledge（S110）**：漏洞知识库——按规则号或关键词查"成因/修法/先例"。
  KB 条目 `rules` 非空 = 本仓扫描器能报的模式；`rules: []` = **本仓未覆盖**的类别
  （路径穿越/竞态/资源泄漏/TOCTOU 等，如实标注，见 VULN-HUNTING 附录 B）。
  检索是关键词匹配（非语义嵌入）。`bug_scan(knowledge=true)` 给每条命中附
  `kb{id,title,fix,covered}`；默认 false 输出与旧版同形。
- **ast_grep（S111）**：可选外部引擎（ast-grep）结构搜索——模式即代码
  （`$VAR`/`$$$` 通配）。**未安装 → 清晰报错 + 安装提示，不静默降级**；实现在
  Rust 侧探测 + argv 直调（Python 薄壳只做校验与转调）；只读搜索，不做 rewrite。
- **code_coverage / module_stability**（metrics 域，S52）：`code_coverage` 用 stdlib
  trace 在子进程跑脚本产出覆盖数据（>10MB 拒读、runner 落沙盒内临时脚本）；
  `module_stability` 以历史改动频率 + 文件规模给稳定性评分（启发式，非缺陷判定）。
- **file_scan（S115/S120）**：签名/熵启发式/哈希/异或层扫描（**非杀毒软件**，如实标注）——
  字面量签名（默认仅 EICAR 测试串）+ SHA-256 黑名单 + 打包熵启发式
  （熵>阈值且 ≥4KB）。**熵计算走 GPU**（≥1MB 实测 31-38×，见 spec/GPU.md；
  auto 按实测交叉点选路，无 GPU 明确降级到 CPU；**不迁 Rust**——逐文件调用时进程
  启动 ~15ms ≥ 计算本身，S120 实测）；`xor_crib` 启单字节异或层枚举
  （≥6 字节已知明文，二进制用 `hex:` 形式；**`xor_engine` 三档如实上报**：
  ≥256KB rust（16MB 485ms）→ ≥128KB gpu（128-256KB 带最优）→ cpu；
  回落原因写 `xor_fallback`，不静默降级）。
- **near_dupes（S117/S118/S119）**：近似重复/同族文件聚类——目录遍历（CPU）+ **bottom-k
  MinHash 指纹（rust 批量 / GPU 两遍选择 / CPU 参考，三档如实上报）** + **精确候选剪枝**
  + Jaccard 阈值聚类。参数 `path`（沙盒内目录）/`ng`（默认 4）/`k`（指纹长度，默认 128）/
  `threshold`（默认 0.8）/`max_files`（默认 100）/`max_file_mb`（默认 64）/
  `engine`（auto/rust/gpu/cpu；auto = exe 在走 rust，否则按实测交叉点）。返回
  `pairs[{a,b,similarity}]` + `clusters[][]` + `sketch_engine{rust,gpu,cpu}`（实际用路）+
  `sketch_fallback`（回落原因，null=未回落）+ `candidates`/`shared_pairs`（剪枝统计）+
  `walk_truncated`（**文件被 max_files 截断时如实标记**）+ `skipped[]`。**近似口径**：
  n-gram 指纹集合相似度，不是逐字节 diff；两两比较用倒排索引 + Jaccard 下界
  I ≥ 2tm/(1+t) 剪枝（**精确，不丢真对**；指纹极短时自动回退全对）。指纹引擎实测：
  rust 批量（`rx-scan sketch`，std::thread 分块）16MB 5.4ms、300×20KB 约 15ms，
  比 GPU 逐文件快 6-13×（GPU 两遍选择 8KB 1.7× → 16MB 551× 是对纯 Python 基线）；
  无 exe/无 GPU 回落 CPU 参考实现。全随机语料 300 文件候选对 44850→0。
  见 spec/GPU.md §二（含"直方图余弦对高熵数据无区分力"的负结果）。
- **结果缓存（S103）**：bug_scan/std_check/ui_check/ast_scan/bug_locate 等纯读
  工具的结果进入**进程内内容寻址缓存**（键 = 工具+参数+cursor+输入指纹；指纹含
  小文件内容哈希，文件一变即失效）。命中返回与冷跑逐字节一致；`__no_cache: true`
  或 `UNIFIED_RX_NO_CACHE=1` 旁路。实测 ast_scan 45ms→3.7ms（约 12×）。大文件
  只按 size+mtime、大仓不缓存等边界写在 tools/cache.py 契约里。
- **dep_graph(resolved=true)（S108）**：附语法级解析边（rx-scan resolvedir）——
  `resolved.imports[{file,line,name,module,to_file,to_line,kind}]`（相对导入按
  层级上溯包、别名绑定、`from pkg import submodule` 回退）、`resolved.external`
  （外部依赖如实分离）、`resolved.unresolved`（name_not_found/star_import）、
  `stats`。默认 false 输出与旧版同形；exe 缺失入 `resolved.error` 不静默。
  本仓对比：文本级引用 93.9% 是假阳性（注释/字符串/子串），解析级 resolved_only=0。
