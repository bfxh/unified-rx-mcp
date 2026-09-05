# 漏洞挖掘能力加强计划（VULN-HUNTING）

> 定稿：2026-09-05，项目所有者要求"找漏洞需要再次的加强，先写文档"。
> 与另外两份文档的分工：**SCAN-POLICY.md 管"怎么扫才标准"（纪律）**，
> **EVAL.md 管"怎么证明有用"（度量门槛）**，本文管"扫得更多、更深、更可信"
> （能力建设）。三份合起来才是完整的扫描体系，互不重复。

---

## 一、现状盘点（2026-09-05，全部有据可查）

### 1.1 已有的家底

| 能力 | 现状 | 出处 |
|---|---|---|
| 静态规则引擎 | bug_scan 规则 **19 条**（Rust 8 / 通用 3 / bevy 引擎 8），分 high/med/low/info 四级 + clue/definite 两类；S83 起全量原生（rx-scan bugscan），手写匹配器在 rust/src/bug.rs，bevy.py 转规则档案 | rust/src/bug.rs、tools/bevy.py（档案） |
| AST 检查 | ast_scan 结构化扫描（Python AST 规则 / JS 词法管线 / Rust fn 归属 + S16 可达性）；S84 起全量原生（rx-scan astscan），实现在 rust/src/astscan.rs，astscan.py 转薄壳 | rust/src/astscan.rs、tools/astscan.py（薄壳） |
| 规则分级纪律 | 线索（clue）不当质量分数用，确定性风险（definite）才计分——避免"文本密度=质量"的假象 | S4-D1 |
| 出口可见性 | bug_scan 交付前按严重度排序，registry 分页不再埋掉新规则命中 | S74（VoxelForge 1808 条实测修正） |
| 常驻攻击工具 | attack 域 3 工具：input_fuzz（每字段 12 类病态输入）/ path_probe（路径逃逸）/ big_input（1MB/深嵌套） | S7 立域、S55 补测试 |
| 扫描纪律 | 四条铁律（禁自扫/副本隔离/静态只是初筛/结论纪律）+ 五步流程 + 四类动态验证用例 | SCAN-POLICY.md |
| 度量门槛 | H3：bug_scan 在标注库上查准率 ≥70% 才算真有用；已建 bench 记分 | EVAL.md、S18 首测 |
| 案例闭环 | "实机踩坑 → 沉淀为规则 → 机器自动拦"已有成功范例（VoxelForge 四轮弹跳床 → avian3d 三规则） | S74 |
| 权力面盘点 | 55 工具 × 授权门/沙盒/真实执行点三列交叉核查法 | S75 |
| 外部深扫 | Mimosa 深度扫描（独立方），58 条发现人工逐条分诊的完整案例 | S73 |

### 1.2 短板（诚实清单）

1. **规则靠踩坑驱动，没有系统覆盖图。** 19 条规则都是"哪里炸过补哪里"，没人说得清
   哪些语言×漏洞类别已覆盖、哪些是空白，更没把"静态原理上查不了什么"写下来。
2. **SCAN-POLICY 的"动态验证"还是口号。** 四类动态用例（协议 fuzz/授权门/路径逃逸/
   fail-closed）写在文档里，但没有一个工具能一键把四类跑全——执行者记得就跑，
   忘了就不跑，和 attack 域立项时批评的"依赖执行者记得"是同一个病。
3. **授权门没有自审。** S75 逐个工具手工查门，靠人眼；下一个新工具忘挂门，没有任何
   机制会报警。attack 域的 input_fuzz 只模糊单字段，不查"该挂门的工具挂没挂"。
4. **误报率没有持续记账。** H3 门槛立了，但标注样本 n=1 的规则占多数，"precision≈1.0"
   如实亮了黄灯；规则越加越多，误报治理没有台账就会劣化（S18 之后的规则都未复测）。
5. **扫描结论没有固定量化格式。** 每轮扫描的 total/by_severity/新规则命中情况散落在
   ROUNDLOG 叙述里，没有可比的数字基线，看不出扫描能力是否在变强。

---

## 二、加强方案（P0 立刻做 / P1 一轮施工 / P2 按需启动）

> 排序原则：先固化"已有的正确做法"（防退化），再补"查不了的"（能力增量），
> 最后才上重型装备。每个条目带**验收标准**——验收不过不算完成。

### P0-a 授权门与权力面自审工具（补短板 3）

给 attack 域加第 4 个工具 `auth_gate_sweep`：一条命令对**全部已注册工具**做双向核查——

- 凡 handler 带 `requires_auth`：无授权调用必须被拒（错误文本含"授权"），
  且 list_tools 的 schema 必须声明 `__authorized`（S72b 契约）；
- 反向：manifest"高权限"段（S75）与实际挂门工具清单必须一致；
- 输出结构化报告：`{总工具数, 挂门数, 漏声明[], 漏拒绝[], 一致性: pass/fail}`。

**为什么是它第一**：S75 靠人眼盘点出 4 个实锤，说明这个方法有效；把方法固化成工具，
新工具一注册就自动被查，从"每次靠自觉"变成"结构上漏不掉"。

验收：`registry.call("auth_gate_sweep", {})` 一键跑全 55 工具；test_s76 常驻断言
`漏声明==[] 且 漏拒绝==[]`；人为造一个"挂门未声明"的坏注册在测试里验证能被抓。

### P0-b 规则入库流程成文（补短板 1 的流程部分）

把 S74 已经走通的"案例→规则"路径写成硬规矩，进本文件与 workflow.md：

1. 任何项目里修掉一个真 bug，必须回答：**这个 bug 能否被一条静态规则识别？**
   （能→当天写规则候选；不能→记入教训库并注明"静态查不了"的原因。）
2. 新规则**三件套**缺一不收：规则本体 + 误报守卫测试（真反例清单，S74 跨语句守卫
   是范本）+ 真仓命中验证（命中处必须人工确认是真雷，且第一页可见——S74 的
   med 排序教训）。
3. 规则消息文本必须写清"为什么这是雷 + 已知误报场景"，让看报警的模型能自行甄别。

验收：S74 的三条 avian3d 规则按此标准逐条补齐档案（作为范例标注在本文件附录）；
下一轮起新规则 ROUNDLOG 必须出现三件套记录，缺件视为未完成。

### P0-c 扫描量化记账（补短板 4、5）

- 固定两个**记账靶场**：本仓 checkout 副本 + VoxelForge crates 副本（均按铁律 2
  用副本，不扫原件所在环境）；
- 每次规则增删或扫描相关修复后，跑 bug_scan 记四格数字：total / by_severity /
  新规则命中数与所在页码 / 误报守卫测试通过数，写进当轮 ROUNDLOG；
- H3 标注库扩容：给现有 19 条规则每条至少配 2 个真实样本（1 正 1 反），
  样本量 n≥2 的规则才允许在 EVAL.md 把黄灯升级。

验收：S76 轮起 ROUNDLOG 出现四格数字；EVAL.md H3 表新增"样本量"列。

### P1（S78 起改道 Rust——用户决策：污点与协议层用 Rust 写，后续大部分功能逐步替换）

- **P1-a 污点轻量版（引擎 = Rust `rx-taint`）**：从"模式匹配"升级到"来源→汇点"
  浅数据流。引擎用 Rust 手写（零第三方 crate，与 Python 侧"纯 stdlib"同纪律）：
  Python 子集词法器（含三引号/f-string/续行）+ 缩进作用域 + 变量污点传播；
  来源 `sys.argv/input()/os.environ/request.*`，汇点 `eval/exec`、`subprocess/*`、
  `os.system`、`open/os.remove/rename/...`、SQL `.execute`；净化器 `os.path.basename/
  .name/secure_filename/int()` 与本仓 `_fs_resolve`（S73 修复方式即净化器）。
  暴露：attack 域 `rust_taint_scan` 工具（Python 壳调 exe，路径过沙盒）。
  验收：S73 深扫重放——修复前快照（git 395e4cd）上 3 条真问题一条不漏；
  55 条误报坐标上，当前 main 的命中数 ≤ 修复前快照命中数的一半。
  落地注记（S78，已过）：精度机制定为**入口点污点模型**——`@tool` 装饰即 MCP 宿主
  可达边界，入口形参=definite 来源，内部 helper 形参=clue 级线索（pass2 实参回溯
  只升不降，宿主来源 argv/env/input/net 恒 definite）；S73 人工"暴露面"triage 从此
  机器化。重放实测 definite=130 ≤ ½ naive(755)=377，3 真全 definite；clue 行仍全量
  报告只分级不隐藏。
- **P1-b 规则覆盖矩阵**：语言（Python/Rust/GDScript/C#/JS）× 类别（注入/路径/并发/
  资源/逻辑/物理引擎陷阱）一张表，逐格标"有规则/原理上查不了/空白"，空白格按踩坑
  概率排优先级。验收：矩阵进本文件附录，"查不了"的格子写明原因（数据流/跨文件/
  运行时状态），不给用户"扫了=没这类问题"的错觉。
- **P1-c 协议层 fuzz 双靶进电池**：协议层本身 Rust 化（`rx-mcp`：零依赖 JSON 解析 +
  MCP stdio JSON-RPC + 转发代理到 python server.py，未来成为宿主入口）；fuzz 电池
  （pytest 常驻）对**两个协议层**都打——畸形 JSON、合法 JSON 但非对象、错型 params、
  超长行/超大串/深嵌套、错型 id、通知后 ping 排水、沙盒外路径的 tools/call——
  全部要求结构化响应不崩、进程存活（继承 input_fuzz 的"绝不崩"标准，打到 stdio
  协议层而非工具层）。验收：电池一键两靶全绿；首跑在 python 侧抓到的崩溃类
  （非 dict 消息 msg.get 崩 / params 数组崩 / 深嵌套 RecursionError 崩）当日修复。
  落地注记（S78，已过）：rx-mcp 以**独立协议实现**落地（解析/分发/tools+ping 直答
  ，通知静默、id 经 i128 全保真）；"转发代理到 python server.py"形态因 Mimosa
  PreToolUse 钩子拦截动态子进程派生而推迟到 S79 评估，不阻塞本项。首跑实抓 4 类
  python 崩溃/污染（上述 3 类 + 通知被误回污染输出流），全部当日修复并入电池回归，
  双靶 32 测全绿。

### P2（方向储备，需要时再启动）

- **P2-a 调用图辅助定位**：dep_graph 已有底子，把"某符号被谁调用/调用谁"接进
  bug_locate 与 code_review 的证据链，减少"就行号报行号"的孤立结论。
- **P2-b 教训库召回**：bug_locate 命中时自动检索 lessons.jsonl 相似历史 bug
  （关键词+文件路径相似度），把"这类问题上次怎么修的"递到修 bug 的模型眼前。
- **P2-c 独立深扫常态化**：每个版本 tag 前，由独立智能体对副本跑一次 Mimosa deep
  扫描 + 逐条分诊存档（seal 编号进 ROUNDLOG），S73 流程从"发生过一次"变成"每个
  大版本一次"。

---

## 三、明确不做（边界纪律）

1. **不引入重型 SAST / 三方扫描依赖**——纯 stdlib 是本仓立仓纪律，扫描能力同样遵守；
   需要重火力时用独立方（Mimosa）而不是把依赖拖进仓。
2. **不扫宿主本体**——铁律 1，一切扫描对副本执行。
3. **不为凑数写低质规则**——一条误报率高的规则污染整个报警流的公信力，
   误报成本大于漏报；宁可用 clue 级别如实标注。
4. **不承诺"扫了=没有"**——静态+动态的边界在 P1-b 矩阵里如实写明，
   扫描结论永远表述为"查到什么/查不了什么"，不说"绝对安全"。

## 四、里程碑

| 轮次 | 内容 |
|---|---|
| S76（本轮） | 本文档落地 |
| S77 | P0-a + P0-b + P0-c（自审工具 / 规则三件套 / 量化记账） |
| S78 | P1-a + P1-c，Rust 化第一步（rx-taint 污点引擎 + rx-mcp 协议层 + 双靶 fuzz 电池） |
| 之后 | P1-b 随规则增长滚动维护；Rust 迁移路线图（下节）逐轮推进；P2 按需启动 |

## 五、Rust 迁移路线图（S78 起，用户决策"大部分功能替换成 Rust"）

原则：**渐进替换、随时可用、每轮全绿**。Rust 侧零第三方 crate（Cargo `[dependencies]`
恒空），与 Python 侧纯 stdlib 同纪律；任何一轮结束时宿主接的入口都必须是完整可用的。

- **S78（已落）**：`rust/` crate 立基——零依赖 JSON 解析/序列化（深嵌套防栈溢出）、
  `rx-mcp` MCP stdio 协议层（转发代理到 python server.py，未来替换入口）、
  `rx-taint` 污点引擎（经 attack 域 `rust_taint_scan` 工具暴露）。Python server.py
  按 fuzz 首跑发现做协议加固。
- **S79+（逐轮）**：按域把工具的实现迁进 Rust（每轮 1–3 个域，先读后写：scan/fs 类
  纯读先迁，attack/lsp 次之，写面最后）。形态：rx-mcp 原生实现该工具后即从
  tools/list 摘掉 Python 版（代理层"原生优先、其余转发"）；Python 侧同名工具保留
  薄壳转调（exe 缺失时报清晰错误，不静默降级）。
  落地注记（S79，fs 读面已落）：首个迁移域 = fs_read/fs_stat/fs_list（rx-fs.exe +
  Python 薄壳，包络契约逐字对齐：resolve 拒绝走 ValueError→ok:false，工具级错误走
  result.error）。**薄壳转调模式使转发代理非必需**——宿主继续用 python 入口即自动
  获得 Rust 实现，"转发代理"维持缓议（单 exe 入口只在全量迁移终点才有意义）。
  迁移实测对齐两处 Python 怪癖：①fs::canonicalize 对不存在路径硬失败而
  realpath(strict=False) 容忍 → 沙盒钳制补"最深存在祖先规范化+余尾拼接"的宽限
  realpath；②fs_list 的 `depth or 1` 把字面 0 静默强制成 1 → Rust 侧归正为
  0=仅根层（契约变化已在 skills/fs.md 声明）。验收：cargo 22 绿
  （fs 13+json 6+taint 3）+ pytest 双解释器全绿（3.14=462+2s / 3.11=464）。
  落地注记（S80，code_search 已落）：第二步 = search 域 code_search 单工具
  （rx-search.exe + Python 薄壳；code_semantic 留 S81）。**迁移契约靠双实现
  对照实验定案**：200 文件上限的截断顺序并非"无契约"——Python os.walk 每层
  先收本目录文件再下钻、目录内 scandir 顺序（NTFS=$UpCase 排序），Rust 初版
  按字母序混排 DFS 使 bench/ 先于根目录源码烧光名额，语料全变、分数系统性
  漂移；对齐后 8 查询（EN/CJK/混合/精确符号/不存在的词）文件+行号+分数
  （±0.001）全 PARITY（tie 顺序按 tie 无关口径比——Python 侧 set 迭代本就
  不稳定）。S12 进程内指纹缓存随 Python 实现退役：短命 exe 无从缓存，实测
  冷调全流程 ~140ms vs 旧 Python 首查 297ms（缓存复查 8ms）。空查询契约
  变化：total=0 → 显式拒绝（"query 必填"）。验收：cargo 32 绿
  （fs 13+json 6+search 10+taint 3）+ pytest 双解释器全绿（3.14=472+2s / 3.11=474）。
  落地注记（S81，code_semantic 已落）：search.py 双工具纯薄壳化完成，S31 纯
  Python 实现与 _SEM_CACHE 退役（冷调 ~330ms vs 旧 ~930ms，缓存成负资产）。
  新增 **stdin 大查询通道**并回补 code_search（S80 潜在缺口）：超
  _QUERY_ARGV_CAP=10000 字符的查询 argv 传 "-"、exe 改读 stdin 全文——
  Windows CreateProcess 命令行上限 32767 UTF-16 码元，旧 test_big_input_smoke
  的 5 万字查询过 argv 即爆；stdin 恒接管防子进程继承宿主 MCP 协议管道。
  四语言七定义匹配器（py/rs/go/js，.ts 非 js 怪癖保留）、trigram 部分名、
  0.02/0.05 双阈值、related 先取 k 再滤——9 查询双实现对照全 PARITY 后才删
  Python 码。验收：cargo 45 绿（fs 13+json 6+search 10+sem 13+taint 3）+
  pytest 双解释器全绿（3.14=483+2s / 3.11=485）。
  落地注记（S82，scan 域三工具已落）：std_check/ui_check/bug_locate 原生化
  （rx-scan.exe + Python 薄壳；bug_scan/ast_scan 的 AST 面留后续轮）。轻正则
  全手写移植（无 regex crate），怪癖逐条保真：godot `$` ≡ 冒号后空白串含换行
  或直达文尾（`[^:]*` 跨行）、unity 无左边界且 `[^)]*` 跨行吞下一行 new、
  文件名兜底把 foo.tsx 捕获成 foo.ts（备选 ts 先于 tsx）、空 needle 命中后
  `direct[-1]["how"]` 覆盖怪癖、\b 按中文也算词字符的 Unicode 口径（123中
  不报）、bevy 死按钮 Marker-Query 跨 system 验证整端口。遍历名额只计代码
  文件；_SCAN_CACHE 对 std_check 退役（短命 exe 无跨调缓存面）。26 案双实现
  对照全 PARITY 后才删 Python 码；冷调 std 真仓 81→69ms、ui 36→22ms
  （bug_locate 小输入 ~10→17ms——spawn 开销盖过轻正则，诚实记账）。验收：
  cargo 58 绿（fs 13+json 6+search 10+sem 13+scan 13+taint 3）+ pytest 双
  解释器全绿（3.14=496+2s / 3.11=498）。
  落地注记（S83，bug_scan 已全量落）：bug_scan 原生化（rx-scan bugscan 子
  命令）——Python AST 规则面由手写迷你解析器 rust/src/pyast.rs 承担（3.14
  语义：缩进驱动/括号续行/f-string PEP 701 区域模型/match 软关键字回退/模式
  匹配全套，零第三方 crate），规则层在 rust/src/bug.rs。7 场景对照（46 文件
  语料三配额/全仓 169 文件 909 条/单文件/非代码/幽灵路径）与旧实现逐字节一
  致后才删 Python 码。坑账：f-string 区域是源切片——`!r`/调试 `=` 必须记
  cut 点真截断；区域内 `(`/`[` 深度防切片冒号误入 spec；ImportFrom 遮蔽键
  用 asname；oracle 自身在被扫仓内，改完 Rust 侧必须重生成 oracle 再比。
  _SCAN_CACHE 全域退役；bevy.py 转规则档案。验收：cargo 72 绿零告警
  （fs 13+json 6+search 10+sem 13+scan 13+bug 9+pyast 5+taint 3）+ pytest
  双解释器全绿（3.14=502+2s / 3.11=504）。
  落地注记（S84，ast_scan 已全量落）：ast_scan 原生化（rx-scan astscan 子
  命令）——pyast.rs 补 col 列号/字符串值解码 CVal/f-string 区域位置（单行
  col+=brace_col+1、多行行偏移列不变），规则层在 rust/src/astscan.rs（py/JS/
  Rust 三管线 + S16 可达性整端口）。14 场景对照逐字节一致后才删 Python 码。
  坑账：panic 正则 \b 在可选点组之前（点形式 match 从 '.' 起）；bytes 只准
  ASCII 约束的是源字符（b"\xef\xbb\xbf" 转义产出合法）；**CRLF 通用换行**
  ——Python open("r") 把 \r\n 读成 \n，exe 保留 \r 曾致行号 +4 全盘漂移，
  读入后归一（探针必须带与真实文件相同的行尾）。scan 域五工具全薄壳。
  验收：cargo 87 绿零告警（+astscan 5 单测+astscan_test 7 集成）+ pytest
  双解释器全绿（3.14=503+2s / 3.11=505）。
  落地注记（S85，app_audit 已落）：appaudit 域唯一纯读工具原生化（rx-audit.exe）
  ——JS 危险面 6 规则+秘密 5 规则手写匹配器、py_splitlines 全集换行、每标签 51
  上限、400 条门、URL 清单/ai 宿主、二进制盘点、asar 提取（sha256.rs 手写
  FIPS 180-4，3 轮扩窗找头+候选基址枚举+SHA256 自标定）整体入 Rust。沙盒门
  strictly_under 在 Rust 侧等价复刻（lenient_realpath+normcase+前缀判定），
  Python 版保留供 app_clean 与 oracle 对照。10 场景对照逐字节一致后才删
  Python 码。坑账：rxrs Value::Int 是 i128（行号/计数/字节全归一）；
  private_key_block 的 [A-Z ]* 贪婪吃光字面量需降序回溯（正则引擎的类内字面量
  回溯语义手写复刻）；asar 错误串带 `_AsarError: ` 类名前缀。**attack 余下
  4 工具是活体自审**（攻击运行中的 Python registry），exe 化会测错对象——
  不迁，属结构性保留。验收：cargo 89 绿零告警（+sha256 2）+ pytest 双解释器
  全绿（3.14=505+2s / 3.11=507）。
  落地注记（S86，app_clone/app_clean 已落）：**appaudit 域 3/3 全薄壳收官**——
  写面原生化（rx-appops.exe clone/clean 子命令），实现 rust/src/appclone.rs。
  授权门结构性留 Python registry（requires_auth + __authorized 于 registry.call
  统一强制，exe 永不自行放权）；沙盒门双语言各一版（appaudit.rs::strictly_under
  转 pub 复用），oracle 钉死等价。**Python 3.14 walk 真值**（探针钉死）：junction
  不再是 symlink（islink=False、悬空也算目录）——有效 junction 克隆目标内容进
  junction 名下、悬空静默剪枝、均不进 skipped_links；Rust 侧 junction 被报成
  symlink 且 read_link 已剥设备前缀 → 文本判别被单测证伪，改 **reparse tag 手写
  FFI**（GetFileInformationByHandleEx，IO_REPARSE_TAG_MOUNT_POINT）。清单根层
  rel=""（旧 Python 语义，清单行 "\t{size}\n"）——首版写成裸文件名被 oracle cmp
  抓获（inventory_digest 假 diff）。os.path.relpath 的 Win32 GetFullPathName 归一
  （成分尾部空格/点剥除）在 errors 显示侧复刻。时间戳走 GetLocalTime FFI。
  py_int 按 Python int() 语义逐字复刻并饱和到 i64（JSON 任意精度防位截断变号）。
  24 步对照（junction/尾点/CRLF/预算四档/SchemaError 前置门/授权门/清理门九态）
  norm 后全 PASS。已知偏差（文档在案）：OS 错误消息文本跨运行时发散
  （[WinError N] vs (os error N)），oracle 掩码只比类名。appaudit.rs 的 S85 walk
  经分析 junction 行为已等价（有效照走/悬空跳过），不改动。验收：cargo 95 绿零
  告警（+appclone 6）+ pytest 双解释器全绿（3.14=507+2s / 3.11=509）。
- **终点（S87 已落）**：宿主接入**不走 rx-mcp.exe 单 exe**——按 S79 决策，薄壳
  转调模式使转发代理非必需，宿主继续用 python 入口即自动获得 Rust 实现；ide/
  ops/attack/game/learn/guard/meta/engine/fs_write 等约 37 个工具结构性留 Python
  （attack 自审/宿主内省/外部进程编排不迁是 S85/S86 既定决策），单 exe 入口只在
  转发代理落地后才有意义 → **"转发代理"维持缓议**，Python 进程不退役（编排器 +
  结构性保留工具 + 测试电池与文档）。落地形态（S87）：config.json mcpServers 追加
  unified_rx（command `python`，args `-X utf8 D:\rj\MCP\server.py`，env
  UNIFIED_RX_SANDBOX="D:\开发;D:\rj\MCP" + PYTHONUTF8=1，enabled）；改前 Yan Agent
  完全关闭，备份 config.json.bak-20260906-pre-unifiedrx，diff 校验仅 mcpServers
  变更，条目扛住宿主启动重写。验证：opencode.log 属懒日志（9月3 后未再写）不可用
  → 改走宿主 GUI"MCP 服务"页实测——"测试 UnifiedRX 连接"→**连接成功，57 个工具**
  （绿勾）；另有宿主外 stdio 冒烟同命令全绿（init 2.14.0 / 57 工具 / fs_read
  沙箱 / app_clone 走 rx-appops.exe / app_clean）。
- **红线**：迁移期间沙盒纪律（fail-closed、`_fs_resolve` 语义）与授权门语义必须在
  Rust 侧等价复刻并通过 `auth_gate_sweep` 同款自审；每轮 pytest + cargo test 双绿
  才准合入。

## 附录 A：规则三件套范例（S74 avian3d 规则档案）

| 规则 | 严重度 | 真仓命中（人工确认） | 误报守卫 | 第一页可见 |
|---|---|---|---|---|
| bevy_phys_manual_support_force | med | vehicle.rs:745（四轮弹跳床案） | ✓ 无误报用例 | ✓（S74 排序后复验） |
| bevy_phys_locked_axes_bits | info | sync.rs:371/397/591 | ✓ | ✓（info 允许后页，已核实在库） |
| bevy_phys_static_with_velocity | low | （VoxelForge 复验清零，规则保留防复发） | ✓ 跨语句守卫 + ::ZERO/matches! 三类 | ✓ |
