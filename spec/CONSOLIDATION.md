# CONSOLIDATION.md —— S126 整合除重 + 上帝对象拆分 + IDE 升级路线（文档先行轮）

> 立场：用户指令「工具整合和除重，先写文档；拆分上帝对象；IDE 整体升级、更能挖漏洞找问题」。
> 本轮**只写文档不动代码**（版本保持 2.43.0，纯文档轮）；实施轮按 §六 顺序逐项过门禁。
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

### 1.4 ide_dead_code 实测（dogfood 当场抓到产品缺陷）
dead 7 + suspect_dynamic 7。人工核验：
- **真死（4）**：`aci.strip_hint`、`cache.reset_stats`、`cache.cacheable`、
  `gpu.literal_scan_cpu`（连同 `literal_scan_gpu` 在 tools/ 内零调用，filescan 走 exe 路径）
- **测试锁定死件（1）**：`appaudit._rx_appops_exe`——仅 tests/test_appaudit.py:250
  kept 名单锁它存在，产品内零调用 → 决策项：接回 or 删+测试同步改
- **误报实锤（缺陷 D1）**：`cache.cache_key` 在 registry.py:407
  `_cache.cache_key(...)` 被属性调用，仍报死——ide_deadcode 引用模型**不数 Attribute
  引用**（`mod.func()` 形态漏采）→ 实施轮修复 + 误报回归测试

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

### C1 walker 除重（实现内，实施轮做）
`scan._iter_files` / `ide_common._iter_files` / `fs_list.walk` / `lsp.walk` /
`ide_deadcode.walk` 五份遍历并存。统一到共享 walker（ide_common 或新 files.py），
语义差异表先行（skip 目录集 / 语言过滤 / 深度），测试锁行为后合并。

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

### P1 `tools/lsp.py`（850 行三职责）
LSP 客户端协议层 ∥ `ide_lsp` 动作分发（fan-out 25）∥ `ide_impact` 三级降级。
拆 `lsp.py`（client）+ `lsp_actions.py`（动作）+ `impact.py`（影响面），工具名全部不变。

### P1 `tools/gpu.py`（658 行）
helper 簇（`_check/_set_arg/_cl/_read_buf`，fan-in 35/34）+ 各域 kernel 混装。
kernel 就近迁移：ngram→neardupes、literal/xor/byte_hist→filescan，gpu.py 留 runtime+
helper。exe/引擎探测逻辑不动。

### P2 Rust 侧（mod 内拆，不动 Cargo.toml）
pyast.rs 2986（parser/scope/extract 分 mod）→ astscan.rs 1776 → nameres.rs 1705
（callgraph 独立 mod）→ taint.rs 1351。Rust 拆分放 Python 侧之后（测试基建更厚再动）。

**拆分轮验收**：pytest 双解释器全绿 + cargo 全绿 + 工具数 69 不变（计数门）+
新文件 ≤400 行软门 + selftest 全对账。

---

## 四、IDE 升级路线（挖漏洞 / 找问题）

### P0-A taint × callgraph 贯通（本轮最高价值）
现状：rust/src/taint.rs（S78）= 过程内浅数据流；nameres（S125）已给调用边。
**升级 = 跨函数污点链**：①污染源在 callee 形参 → 经调用边追到 caller 的汇点；
②caller 传污染实参 → callee 内汇点。设计要点：净化器跨界规则如实（callee 内净化
不返传视为未净化）；输出带 edge 级证据链（污点从哪进来、经哪几跳、到哪个汇点）；
验收 = 跨文件污点夹具红转绿 + 现有过程内用例不回归。

### P0-B ide_dead_code Attribute 盲区修复（D1，dogfood 实锤）
引用模型补 Attribute 引用采集 + `cache_key` 误报回归测试。这是"找问题"工具自己
先过硬——量尺不准，量出来的死代码清单就不可信。

### P1-A 覆盖率 × 调用图风险榜
`code_coverage`（stdlib trace）× `ide_callgraph` fan-in → **高扇入 × 零覆盖**优先级
清单 = SCAN-POLICY 的自动化落地（拆分/补测排序不再拍脑袋）。结果存 JSONL，
与 ide_health_trend 同库出趋势（顺带还 HARDENING"覆盖率趋势"欠账）。

### P1-B ide_impact 接调用面
三级降级链加第四档：LSP references（引用）→ 名字解析（引用）→ **调用图（调用）**，
engine 字段如实标注档位——引用≠调用分层不混（HARDENING 缺口清单既定候选）。

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

1. D1 ide_deadcode Attribute 盲区修复（量尺先准）
2. P0 scan.py 拆分 + C1 walker 统一
3. 死代码清理（§1.4 真死 4 件，删前测试同步）
4. P0-A taint×callgraph
5. P1-A / P1-B / P1 拆分（lsp/gpu）
6. P2-A / P2-B / C2 / P3

每步通用门禁：测试先行或同步迁移、注册名与工具面不破坏（A 级项需 deprecation
说明）、计数门 69 不变、selftest 对账、pytest+cargo 双绿才准合入（pre-commit 强制）。

## 六、不做边界
不改协议层/server.py 形状；不引入第三方 pip 依赖（外部工具一律探测薄壳）；
不动 requires_auth 面除明确标注项；Mimosa 审计欠账（scanner_enobufs）未还清前，
任何输出不宣称"项目安全"。

## 七、与 HARDENING 缺口清单映射
真调用图（S125 已兑）→ 本文接棒：ide_impact 调用面（P1-B）、ide_dead_code 辅助证据
（P0-B 反向：量尺先修）、类型检查集成（P2-A）、覆盖率趋势（P1-A）。
本文档本身 = HARDENING §六"下轮候选"的展开与排序。
