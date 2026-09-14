# metrics 域（code_coverage / dep_graph / module_stability）

- **S136 组轴归位**：本三件实现在 tools/metrics.py（S52 自立"代码质量度量域"），
  此前注册挂在 scan 组——组/模块两轴错位（DESIGN-REVIEW M2 实锤），S136 一次做全：
  新增 metrics 组收本三件 + `project_health` 自 ops 归 scan。计数门全套随动。
- **组内选型**：量"覆盖/依赖/稳定性"用本组；量"代码模式/缺陷/凭据"用 scan 组。

- **code_coverage / module_stability**（metrics 域，S52）：`code_coverage` 用 stdlib
  trace 在子进程跑脚本产出覆盖数据（>10MB 拒读、runner 落沙盒内临时脚本）；
  `module_stability` 以历史改动频率 + 文件规模给稳定性评分（启发式，非缺陷判定）。
- **dep_graph(resolved=true)（S108）**：附语法级解析边（rx-scan resolvedir）——
  `resolved.imports[{file,line,name,module,to_file,to_line,kind}]`（相对导入按
  层级上溯包、别名绑定、`from pkg import submodule` 回退）、`resolved.external`
  （外部依赖如实分离）、`resolved.unresolved`（name_not_found/star_import）、
  `stats`。默认 false 输出与旧版同形；exe 缺失入 `resolved.error` 不静默。
  本仓对比：文本级引用 93.9% 是假阳性（注释/字符串/子串），解析级 resolved_only=0。

