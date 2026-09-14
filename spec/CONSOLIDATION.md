# CONSOLIDATION.md —— S126 整合除重 + 上帝对象拆分 + IDE 升级路线（文档先行轮）

> 立场：用户指令「工具整合和除重，先写文档；拆分上帝对象；IDE 整体升级、更能挖漏洞找问题」。
> 本轮**只写文档不动代码**（版本保持 2.43.0，纯文档轮）；实施轮按 §五 顺序逐项过门禁
> （S127 起进入实施，§八 记录）。
> 所有数字来自 2026-09-14 对本仓的 dogfood 实测（用自家工具盘自家家底，同时实战检验
> S125 交付物），复跑方法随条目给出。

---

## 一、证据（dogfood 实测，可复跑）

### 1.1 工具面全景
69 工具 / 12 组：appaudit(3) attack(5) engine(2) fs(4) game(2) guard(2) ide(22)
learn(1) meta(5) ops(5) scan(15) search(3)。
复跑：`python -c "import tools; from registry import _TOOLS; ..."`（dump name/group/desc/params）。

### 1.2 行数盘点（wc -l，top 部分）

| 文件 | 行数 | 备注 |
|---|---|---|
| rust/src/pyast.rs | 2986 | Rust 侧最大 |
| rust/src/astscan.rs | 1776 | |
| rust/src/nameres.rs | 1705 | S125 扩至 |
| rust/src/taint.rs | 1351 | |
| tools/lsp.py | 850 | Python 侧最大 |
| tools/gpu.py | 658 | |
| tools/scan.py | 650(审计口径545) | **30 commits/30d 全仓最高** |
| registry.py | 476 | 分发枢纽（设计如此，非拆分对象） |

### 1.3 ide_callgraph 实测
全仓（max_files=600，183 文件 / 1649 节点）：calls **13831** = resolved **2685** +
unresolved **7476** + builtin **3670**，stitched 895，resolution_rate **0.264**。
by_reason 四大头：external 3026 / receiver_var 3168 / attr_chain 999 /
self_attr_missing 112（与 S125 边界模型一致：无类型推断 + 外部依赖是主因，如实不美化）。
tools/ 内部（40 文件）：
- **fan-in 榜**：`tools.fs._resolve` 47（沙盒门人人共享，设计如此）、`tools.gpu._check` 35、
  `tools.gpu._set_arg` 34、`tools.ide_doctor._run_check` 8、`tools.scan._rx_scan_call` 8、
  `tools.lsp._as_uri` 8
- **fan-out 榜**：`gpu.ngram_bottomk_gpu` 31、`lsp.ide_lsp` 25（动作分发大函数）、
  `ide_doctor.ide_doctor` 18（纯聚合，合理）、`ide_test._run_pytest` 16、`breaker.check` 13
- **环检出**：4 个全部良性（fs_list.walk / lsp.walk 自递归、ide_build watch 循环、
  metrics._find_cycles.dfs）；模块级 import 依赖另由 dep_graph 证实无环

### 1.4 ide_dead_code 实测（**含 S127 误诊更正**）
首次 dogfood 只扫了 `tools/`（40 文件）→ dead 7 + suspect 7，当时据此下了"缺陷 D1"
结论。**S127 复扫全仓（183 文件，path=仓库根）后更正：D1 系扫描范围误诊**——
`cache.cache_key` 的引用者在仓库根 `registry.py:407`、`cacheable/reset_stats` 在
tests/、`literal_scan_*` 在 bench/tests，全部**不在 tools/ 内**；工具引用模型本就
同时数 Name 与 Attribute（`_shorthand_refs` 的 attrs 集合，S123 设计如此）。
教训（已入 ROUNDLOG）：**判死范围必须 = 全仓**，子目录扫描结果不得下"全库零引用"
结论。全仓真实结论：
- **真死（1，产品代码）**：`aci.strip_hint`（S105 时期标注"测试辅助"，引用者早已
  退役）——S127 已删并加不回潮测试；`tools/` 侧原报的其余三件全部为误报；
- **冻结资产（6，不动）**：bench/manual_snaps 下 VoxelForge 历史快照的
  `exposed_faces/align_offset_to_grid/mount_points_to_ron`——快照按定义冻结，
  零引用≠可删除；
- **suspect_dynamic（3）**：`_rx_appops_exe`（仅测试 kept 名单锁存在，保留决策待定）、
  `_strictly_under`（Rust 侧镜像同名，Python 侧零引用但为文档锚点，保留）、
  `bevy_rules`（S83 规则档案，注释即引用，保留）。

### 1.5 module_stability watch 榜（git 频率 × 测试 × 覆盖）
`tools/scan.py` 545行/30 commits —— **头号上帝对象实锤**；`server.py` 305/103（版本锁步
导致高频，正常）；`registry.py` 375/17；`tools/ide.py` 19行/16 commits（纯 re-export 枢纽，
提交频=计数门联动，不是问题）。

---

## 二、重叠矩阵与整合方案（分级 A/B/C）

> 分级：A=工具面合并/迁组（破坏面，需 deprecation 周期）；B=实现已共享、补文档划界；
> C=实现内除重（不碰工具面）。

### B1 全家桶入口四件套（选型表，进 skills）
| 入口 | 组成 | 定位 |
|---|---|---|
| `project_scan` | bug_scan+std_check+ui_check 三路裸组合 | 快速三路扫（最便宜） |
| `project_health` | 同三路 → 0-100 分 | 只要分数不要明细 |
| `code_review` | bug+安全+复杂度+todo+dup+untested，lens 可选、mode=diff | 改动/评审视角 |
| `ide_doctor` | 六项聚合（含 build/test/dep/stability）→ verdict+top 清单 | 一条命令基线；`ide_multi_check`=其多项目循环 |

实现层已全部共享 runner（ide_doctor "纯聚合不造新检测"、project_health 引 scan 模块）——
**重复在入口面不在代码**。处置：不合并工具；①skills 加选型表；②`project_health` 组错位
（在 ops 不在 scan）→ A 级低优先迁组或并入 project_scan 加 `score=true`（待定，不急）。

### B2 检索四件套（选型表，进 skills/search.md）
`code_search`（BM25 行级，标识符/中文都行）｜`code_semantic`（tf-idf 定义级，找"谁实现"）｜
`engine_query`（codegraph 优先、BM25 降级）｜`repo_map`（PageRank 骨架，"该看哪些定义"）。
`locate_edit` 与 code_search 有重叠 → 划界：locate_edit=编辑前引导（配 code_context 用），
保留。四件套不合并，写选型表。

### B3 编辑对
`ide_batch_edit`（跨文件行块）vs `ide_edit_multi`（单文件多段）同在 ide_edit.py，
匹配引擎共享（实现层除重已完成）；语义不同（跨文件 vs 单文件多段+occ），工具面都留，
文档写明差异。

### C1 walker 除重（**S127 已做**，含范围修正）
复查实际形态：模块级**文件遍历器是 3 份**（`scan._iter_files` /
`ide_common._iter_files` / `ide_deadcode._walk_py`）——收敛为
`tools/filewalk.iter_files` **唯一 os.walk 实现**，各域保留参数化 profile
（语言表 / skip 集 / 单文件支持），语义逐字不变 + 防回流锁测试。
文档原列的 `fs_list.walk` / `lsp.walk` 实为**树递归闭包**（目录树/JSON 树），
非同族不并；**C1b 候选**：cache/filescan/neardupes 的内联 os.walk 与
5 份 `_SKIP_DIRS` 副本（语义各异需逐一对齐，另含 code_review 的 md5→sha256
候选——md5 用途为内容分组非完整性，平移轮遵守零改动纪律未顺手改）。

### C2 组归位（A 级低优先）
`breaker_status/reset` 在 meta（更应 guard）；其余归位检查通过
（capability_manifest/hallucination_guard 在 guard 合理、gpu_status 在 meta 合理）。

### C3 ops 内划界
`scan_log`（扫描审计流）vs `usage_stats`（工具使用统计）职责不同，都留，文档一句划清。

---

## 三、上帝对象拆分计划（SCAN-POLICY：拆分大于测试）

### P0 `tools/scan.py`（650 行双域一文件）
ide_outline 实测结构：`_rx_scan_exe/_rx_scan_call`（exe 桥）+ bug/std/ui/locate/
project_scan（Rust 壳域）∥ `_symbol_spans/_func_spans/_complexity_findings/
_test_mod_regions/_review_file/_dup_file_findings/_untested_findings/
_git_changed_ranges/code_review`（纯 Python 评审域 ~270 行/11 函数）。
**拆法**：`scan.py` 留 Rust 壳域；`code_review.py` 整体平移评审域。注册名不变、
逻辑零改动（git mv 式平移）、导入面只动 tools/__init__.py 与测试 import。
与 C1 walker 统一同轮做。

### P1 `tools/lsp.py`（850 行三职责）（**S129 已兑**）
拆为 `lsp.py`（client 核心 507 行）+ `lsp_actions.py`（ide_lsp 动作分发）+
`impact.py`（影响面 + 调用面档），注册名/语义零变化；测试引用面按包属性访问
纪律平移（`_SESSIONS`/`_Session`/`_from_utf16_col`/`_apply_text_edits`/
`validate_content` 留 client——lsp_actions 对状态一律模块属性访问，test_s60/
test_s99 的 monkeypatch 面继续生效）。

### P1 `tools/gpu.py`（658 行）（**S130 已兑**）
拆分：gpu.py = 运行时（加载/上下文/编译缓存/参数/读回 + CROSSOVER +
pick_mode + gpu_status）；kernel 就近迁域——literal_scan/byte_hist(+entropy)/
xor_crib_scan → filescan.py，ngram_bottomk/ngram_hashes/bottom_k/jaccard →
neardupes.py（CPU oracle 与内核同行；公开名不变）。引用面全量随动：
filescan/neardupes 内部调用点、6 个测试文件 + bench（含二次漏网的
test_s117/118/119/120 一次扫清——首轮只改了显式清单里的两件，
跑测试红出其余四件）。

### P2 Rust 侧（mod 内拆，不动 Cargo.toml）
pyast.rs 2986（parser/scope/extract 分 mod）→ astscan.rs 1776 → nameres.rs 1705
（callgraph 独立 mod）→ taint.rs 1351。Rust 拆分放 Python 侧之后（测试基建更厚再动）。

**拆分轮验收**：pytest 双解释器全绿 + cargo 全绿 + 工具数 69 不变（计数门）+
新文件 ≤400 行软门 + selftest 全对账。

---

## 四、IDE 升级路线（挖漏洞 / 找问题）

### P0-A taint × callgraph 贯通（**S128 已兑**）
交付：跨文件污点链——实参→形参、污染返回值→lhs 的跨界不动点 + 名解析消费 nameres
调用图（别名/模块属性调用可连）+ 唯一名连边（多义跳过计数）+ origin 链证据
（flow=cross）+ cross=false A/B 开关。REPLAY A/B 与边界见 VULN-HUNTING P1-a 落地
注记（S128）；实现与测试清单见 ROUNDLOG S128。**未竟**：字段/路径敏感、getattr
动态面（附录 B 仍标 ⚠️）。

### P0-B ide_dead_code Attribute 盲区修复（**S127 撤销：误诊，见 §1.4**）
原"D1 缺陷"经全仓复扫证伪——引用模型本就数 Attribute，假阳性系扫描范围错误。
工具零改动；教训（判死范围=全仓）已入 ROUNDLOG S126 更正条目。

### P1-A 覆盖率 × 调用图风险榜（**S129 已兑**）
`ide_risk_rank`（ide 域第 23 件，总 70）：高扇入（调用图可解析调用边，按被调定义
to_file/to_line 聚合）× 无测试（静态文件约定代理——与 code_review 覆盖透镜共用
`test_candidates` 口径；rust 认内联 cfg(test)；**非实测覆盖**，实测仍走
code_coverage）→ score=扇入×(无测试?2:1) 排序；JSONL 记账 +
`mode=history` 同 root 趋势（总扇入/无测试数首末对比，还 HARDENING 覆盖率趋势
欠账的静态版）。

### P1-B ide_impact 接调用面（**S129 已兑**）
独立 `calls` 段（不混入降级链的 engine 标注）：Rust 调用图调用边，
**定义点对齐**（to_file/to_line）消歧，未对齐退文件+短名并在 note 如实标注；
`calls=false`/exe 缺失该段如实缺席（fail-open 有界，不拖垮主结果）。

### P2-A 外部 linter 编排
ruff/pyflakes/mypy 能力探测薄壳（同 pylsp/ast-grep 惯例：装了就用、没装清晰报错
不静默降级），输出进 ide_diagnostics 统一形状（severity 归一、行号 1-based）。
零 pip 依赖红线不破——外调不内嵌。

### P2-B bug_scan 规则扩容（每条配 vuln_knowledge 条目，S110 联动惯例）
`shell=True` / `pickle.loads` / `yaml.load` 无 Loader / md5·sha1 用于密码 /
SQL 字符串拼接 / `mktemp` 临时文件竞态 / zip-slip 解压路径 / `except:pass` 吞关键错误。

### P3 attack 巡航
big_input × input_fuzz × auth_gate_sweep × path_probe 已各有其职，缺一键编排入口：
cruise 模式跑全攻击面自检并出统一报告（attack 域内薄聚合，同 ide_doctor 惯例）。

---

## 五、实施顺序与门禁（实施轮按此走）

1. ~~D1 ide_deadcode 修复~~ → **S127 更正：D1 系误诊（见 §1.4），量尺无需修**
2. ~~P0 scan.py 拆分 + C1 walker 统一~~ → **S127 已兑**（见 §八）
3. ~~死代码清理~~ → **S127 已兑**（真死收敛为 1 件 strip_hint，已删加锁）
4. ~~P0-A taint×callgraph~~ → **S128 已兑**（见 §四 P0-A 与 §八）
5. ~~P1-A~~ / ~~P1-B~~ / ~~P1 拆分（lsp）~~ → **S129 已兑**；**gpu 拆分 + C1b
   剩余（cache 记账交织 walk 如实不并）→ 下一轮第一优先**
6. ~~P2-A~~ → **S130 已兑**；~~P2-B~~（八规则 + KB 联动）/ ~~C2~~（breaker 归位）→ **S131 已兑**；**P3（attack 巡航）待做**；另开 H1/H3/M4（设计评审高优先项：授权分级立文 / 组合透传检查器 / 家族选型表）→ 见 spec/DESIGN-REVIEW.md

每步通用门禁：测试先行或同步迁移、注册名与工具面不破坏（A 级项需 deprecation
说明）、计数门 69 不变、selftest 对账、pytest+cargo 双绿才准合入（pre-commit 强制）。

## 六、不做边界
不改协议层/server.py 形状；不引入第三方 pip 依赖（外部工具一律探测薄壳）；
不动 requires_auth 面除明确标注项；Mimosa 审计欠账（scanner_enobufs）未还清前，
任何输出不宣称"项目安全"。

## 七、与 HARDENING 缺口清单映射
真调用图（S125 已兑）→ 本文接棒：ide_impact 调用面（P1-B）、ide_dead_code 辅助证据
（S127 更正：量尺本就数 Attribute，见 §1.4）、类型检查集成（P2-A）、覆盖率趋势（P1-A）。
本文档本身 = HARDENING §六"下轮候选"的展开与排序。

## 八、S127 实施记录（第一实施轮：拆分 + 除重 + 量尺更正）

**已兑：**
1. **误诊更正**（§1.4）：全仓复扫证明"D1 缺陷"不存在——是 dogfood 扫描范围错误。
   工具零改动，教训入档（判死范围 = 全仓）。
2. **P0 拆分**：`tools/scan.py` 650 行双域 → `scan.py`（壳域 ~210 行）+ 新建
   `tools/code_review.py`（评审域，含 code_review + 11 透镜助手 + 3 常量组）。
   注册名/透镜语义/输出形状全部不变；`_iter_files` 调用改走 filewalk；
   两侧同批清理两个零引用死常量（`_PLACEHOLDER_WORDS`/`_RE_FUNC_START`，S83 遗留）。
3. **C1 除重**：新建 `tools/filewalk.py`（唯一 `os.walk` 实现）——三份遍历器
   （scan / ide_common / ide_deadcode）全部委托到参数化 profile，语义逐字保留
   （含"单文件无条件产出"兼容语义）；锁定测试：filewalk 恰 1 处 `os.walk(`，
   另两家 0 处。
4. **死代码清理**：`aci.strip_hint` 删除（全仓零引用核实后）+ 不回潮测试。
5. **测试**：`tests/test_s127_split.py` 13 例（三 profile 行为等价 / 防回流锁 /
   注册面契约 / code_review 烟测 lens 过滤）；`test_s61`/`test_s70` 的 import
   随平移更新（`_func_spans`/`_symbol_spans` → tools.code_review）。
6. **记录**：ROUNDLOG S126 条目附更正 + S127 实现条目；HARDENING §六指针同步。

**教训归档**（本轮第二条通用课）：
- "全库零引用"级别的结论，扫描范围必须 = 全仓；子目录 dogfood 只给子目录事实。
- 平移拆分的纪律：先 grep 全部外部引用面（tests/bench 的 import 与属性访问），
  再动文件；死常量清理前先验证零引用（`_RE_FUNC_START` 定义并存两轮未被发现）。
- Mimosa hook 两次拦截均为**测试夹具的扫描器诱饵**（凭据字样）与 md5 建议——
  前者改用既有 s44 模式（os.system 分支）、后者挂 C1b 记录不顺手改（平移零改动）。

**S128 实施记录（P0-A，第二实施轮）**：`rust/src/taint.rs` 跨文件化——TSrc/Hit/
Finding 加 `origin` 链字段（三处赋值传播 + expr_taint 六处构造全线程）；scan_path
拆两段（先全量分析保留 Analyzer，再跨文件传播，最后统一产出）；`cross_file_propagate`
不动点 ≤4 轮（只升级不降级 + 同级别只补链证据防振荡 + 环安全）；`callgraph_targets`
消费 nameres::callgraph_dir 的边（(file,line) → callee 基础名，同行多目标不猜），
无解析回退文本名；`--no-cross` CLI + 工具 `cross` 参数（默认 true）。测试：rust
taint_test 3→7 例（链/净化不失效/调用点净化/多义计数/互递归终止/别名经调用图）；
Python 工具测试 +1（别名链 + 开关）；REPLAY 新增 `test_s128_cross_file_is_strict_superset`
（超集不变量 + origin 必带 + A/B 记账行）。A/B（快照 395e4cd）：all 627(+0) /
definite 131(+1) / cross_flows 7 / ambiguous 80——净新增 0 如实入档。已知未竟：
字段/路径敏感、getattr 动态面（附录 B 维持 ⚠️）。

**S129 实施记录（P1-A + P1-B + lsp 拆分 + C1b，第三实施轮）**：
- **lsp.py 拆分**：850 → client 507（`_SESSIONS`/`_Session`/`_from_utf16_col`/
  `_apply_text_edits`/`validate_content` 全留 client）；`lsp_actions.py`（ide_lsp，
  对 client 状态一律模块属性访问——既有 monkeypatch 面零破坏）；`impact.py`
  （降级链 + 新调用面）。测试引用面 5 处属性 + 4 处字符串 setattr 全数更新
  （test_s109/test_s99/test_s60/test_r3）。门连锁：模块尺寸门最大值从 lsp.py 850
  降到 gpu.py 658（test_s113 注释同步实测值）。
- **P1-B 调用面**：`ide_impact` 输出新增 `calls` 段——engine=rust:nameres-callgraph，
  **定义点对齐**（to_file/to_line 精确圈定；未对齐退化文件+短名 + note 标注），
  `call_sites`/`callers`（file/line/caller）/`def_line_aligned`；`calls=false`
  关闭；exe 缺失/报错如实缺席。契约测试 4 例（跨文件 from-import 边实锤）。
- **P1-A 风险榜**：新工具 `ide_risk_rank`（70 工具 / ide 23）——扇入×无测试
  （score=扇入×(无测试?2:1) 公式随输出透明）；`test_candidates` 自 code_review
  提取共享（真除重）；JSONL 记账 + history 趋势（空历史/同 root 过滤/首末 delta）；
  测试 5 例（排序权重/测试排除开关/记账回读+delta/exe 缺失清晰错/schema+沙盒）。
  计数门全套随动：README ×4、PANORAMA、skills/README、skills/ide.md、test_v2、
  test_s127、README 对比表"70 个组合工具"。
- **C1b**：filescan/neardupes walk 收敛 filewalk（新增 `sort_files` 参数保留每层
  排序语义 + `TOOLCHAIN_SKIP_DIRS` 唯一副本）；neardupes 截断如实（多取 1 判截断，
  恰好上限不标）；cache.py 记账交织 walk **如实不并**（预算/哈希与遍历交织，参数化
  会失真）；行为锁进 test_s127（含 venv 跳过/排序/截断三断言）。
- **gpu 拆分延后**（理由见 §三 P1 gpu 段）：下一轮第一优先。

**S131 实施记录（P2-B + C2 + 设计评审，第五实施轮）**：
- **P2-B 八规则**（bug.rs，AST+行级）：py_shell_true（keyword 节点行定位，跨行亦可判）
  / pickle_loads / yaml_unsafe_load（Loader= 即不报）/ weak_hash_password（仅口令语境）
  / sql_concat（BinOp/JoinedStr）/ mktemp_race / zip_extractall / except_pass（有类型
  except+pass，与 bare_except 互补）；rust 测试 +2 例（命中与不报双侧）；KB 联动：
  新增 kb-deserialization/kb-weak-hash/kb-mktemp/kb-zip-slip 四条 + 扩三条例目规则号，
  kb-sql-inject 摘「未覆盖」标；test_s110 KNOWN_RULES 同步。
- **C2 组归位**：breaker_status/reset meta→guard（挂门语义与 guard 同族）；
  skills meta.md→guard.md 迁移、README/PANORAMA/skills-README 计数随动。
- **设计评审**：spec/DESIGN-REVIEW.md（70 工具 / 12 组 / 36 模块 / 18 挂门实测）
  ——高优先 H1 授权分级立文（S130 事故根因）/ H2 root-vs-path 词汇分裂（15:28）/
  H3 组合透传检查器；中优先 M1 中文输出键 3 件 / M2 metrics 组轴错位 / M3 kind·engine
  一词多义 / M4 选型表文档债；低优先 L1 子进程沙盒边界等记录在案。

**S130 实施记录（gpu 拆分收尾 + P2-A，第四实施轮）**：
- **gpu 拆分**（§三 P1，上轮延后项）：gpu.py 658 → 运行时约 400 行；kernel 三簇
  就近迁 filescan（472）/neardupes（467）；CROSSOVER 表留 gpu.py 完整（含 ngram
  条目，注释合并）；tests/bench 引用面 8 文件全量随动，GPU 系 46 例全绿；
  尺寸门最大件：gpu.py 658 → lsp.py 507（comment 同步）。
- **P2-A 外部 linter 探测薄壳**（§四 P2-A 已兑）：ide_diagnostics 增 ruff
  （--output-format=json --no-cache）→ 无则 pyflakes 回退 + mypy（类型面，
  MYPY_CACHE_DIR 钉 TEMP）；三个解析器独立成函数可单测（E9*/F82* → error）；
  能力缺席/超时/失败一律进新 skipped 列表。
- **实锤缺陷顺带修（S130 发现）**：ide_diagnostics 的 clippy 透镜在生产路径上
  是死的——内层 registry.call(ide_build) 无 __authorized 被授权门拒绝，又被
  except 静默吞成 engine=none, total=0（真机实测）。修：本工具升**执行类挂门**
  （schema 声明 __authorized + 必填）+ 内层调用显式传授权 + 拒绝/失败进 skipped；
  callers 随动（swe_repair ×2、test_ide_diag 往返）；新增回归门
  test_clippy_refusal_is_visible_not_silent。这是 S55 静默吞家族的授权门变体
  ——同一病灶换了个触发面。
