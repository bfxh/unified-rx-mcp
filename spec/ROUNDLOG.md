# ROUNDLOG —— 每轮推导/决策/证据记录

规范：每轮三要素（任务/决策/证据）；本文件由 bench/log_round.py 追加，人工可补充。

## S38 · 三路信号 A/B 实测
- 项目：unified-rx-mcp｜时间：2026-08-29T01:53
- 决策：signals vs plain 同起点配对；负结果如实入账不硬凑
- 证据：28 任务×2 臂：net lift 0（A -1/B +1），median 16.9s vs 15.6s
- 提交：74714ba

## S39 · ponytail skill 安装 + 每语言/每域 skill 门禁 + 会话记录规范
- 项目：unified-rx-mcp｜时间：2026-08-29T01:53
- 决策：MCP 对外必须有 skill（CI 门禁强制）；记录规范跨项目通用
- 证据：skills/ 12 域 + 6 语言文档；manifest gate 4 测试

## S40 · ponytail-review 全仓审计 + WSL 补完收官
- 项目：unified-rx-mcp｜时间：2026-08-29T02:43
- 决策：死代码 AST 引用计数扫描 + 人工判定；WSL 长尾（astropy-8872 三重障碍）如实挂账不硬凑
- 证据：砍 4 死件 + 误伤恢复（_UI_PATTERNS 从 git）；feasible 45/47=95.7%；225 passed
- 提交：c635b5f

## S41 · astropy-8872 原生 clone 攻坚
- 项目：unified-rx-mcp｜时间：2026-08-29T04:51
- 决策：原生 clone+helpers 铺满+版本探针全通，egg_info 静默死为终点——止损挂账；.wslconfig 调优保留
- 证据：setup.py --version RC=0；egg_info 无输出死亡 ×3（OOM 已修仍死）；225 passed
- 提交：待提交

## S41-note · astropy-8872 提交补账
- 项目：unified-rx-mcp｜时间：2026-08-29T04:51
- 决策：S41 commit a103daf
- 证据：225 passed
- 提交：a103daf

## S42 · WSL 测试假跑修复（用户逼出来的）
- 项目：unified-rx-mcp｜时间：2026-08-29T09:59
- 决策：tail 尸检发现  未定义 → 全部 WSL FTB 假跑；修复脚本模板 + guard 硬化 + 4 env 重建
- 证据：fake-tail 0/45；base_bad 17 全部真因分类；verified A8/B6 真实口径；225 passed
- 提交：待提交

## S42-note · S42 commit 补账
- 项目：unified-rx-mcp｜时间：2026-08-29T09:59
- 决策：假跑修复提交
- 证据：225 passed
- 提交：45a4170

## S43 · 守卫全面硬化（S42 教训制度化）
- 项目：unified-rx-mcp｜时间：2026-08-29T10:20
- 决策：能力探针替代存在性检查；infra 故障与测试失败结构化区分；消费端 skip 语义
- 证据：230 passed（+5 守卫测试）；verify/repair infra skip 端到端

## S43b · MCP server 缺省沙盒全开修复（端到端验收抓出）
- 项目：unified-rx-mcp｜时间：2026-08-29T12:38
- 决策：缺省 fail-closed 恢复 S0 语义；可信宿主显式配置
- 证据：未配置=拒 ✓ 配置后 12/16 工具真走查通过；230 passed
- 提交：待提交

## S44 · code_review 多透镜评审工具
- 项目：unified-rx-mcp｜时间：2026-08-29T13:06
- 决策：聚合 bug/security/complexity/todo 四透镜 + diff 改动行模式；复用 bug_scan 不另造轮子
- 证据：234 passed（+9 测试：透镜/diff/复杂度/todo）；本仓自评 12 复杂度热点全属实；scan.md 边界已记
- 提交：待提交

## S45 · code_review 12 热点全清零（自食其果闭环）
- 项目：unified-rx-mcp｜时间：2026-08-29T14:05
- 决策：真问题重构（单体拆分/参数砍）、假阳性修工具（括号深度感知）、阈值校准留注释
- 证据：code_review(ide.py) 12→0；234 passed；工具链自验证闭环
- 提交：待提交

## S46 · clippy/复杂度接进修复轮回喂
- 项目：unified-rx-mcp｜时间：2026-08-29T15:23
- 决策：warning 级放行（clippy 同通道）+ code_review 复杂度透镜拉触碰文件；error 优先排序
- 证据：234 passed；diag_section 双级断言；S38 A/B 基线保留可复测
- 提交：待提交

## S47 · clippy/复杂度信号 A/B 复测
- 项目：unified-rx-mcp｜时间：2026-08-29T16:08
- 决策：三轮独立 sweep 聚合净 −2——信号维度研究收口，不再 revisit
- 证据：56 文件 ×2 变体重跑；A +1/B −2；168 配对 run 聚合 signals 13 vs plain 19
- 提交：待提交

## S48b · ide_break 自模拟验收
- 项目：unified-rx-mcp｜时间：2026-08-30T00:55
- 决策：沙盒内真实断点会话；runner 缺 cwd sys.path 的第三真 bug 修复
- 证据：locals {speed:7, step:14} + 栈帧捕获成功；225→230+ 测试全绿
- 提交：本次

## S50 · VF3 dist 陈旧副本清理 + bench 自扫
- 项目：unified-rx-mcp｜时间：2026-08-30T01:13
- 决策：只删字节级相同且有 canonical 的 dist 副本（10 个，构建产物）；docs 归档与 bench one-off 不砍
- 证据：清理后复扫 duplication 剩 2 对；234 passed

## S51 · 五项 IDE 增强全落地
- 项目：unified-rx-mcp｜时间：2026-08-30T01:35
- 决策：LSP didChange 增量推送 + code_review base 分支 + 条件断点 + watch 模式 + 诊断历史 JSONL
- 证据：235 passed；全部走真沙盒路径

## S52 · 全电池验收 + 装饰器错绑修复
- 项目：unified-rx-mcp｜时间：2026-08-30T01:53
- 决策：AST 拆分把 @tool 绑到 _lsp_file_diags 而非 ide_diagnostics——registry._TOOLS 逐工具验证 handler 名应为拆分后标准步骤
- 证据：VF3 cargo 0 错/clippy 0/bug_scan 492 info/装饰器修复后 ide_diagnostics 正常；235 passed
- 提交：本次

## S53 · 维稳版部署 D:\rj\MCP + 工作流规范
- 项目：unified-rx-mcp｜时间：2026-08-30T02:23
- 决策：main 合入 feat 分支 + tag v2.3.0 + clone 到 D:\rj\MCP + workflow.md 固化流程；235→237 passed
- 证据：D:\rj\MCP pytest 235 passed ✓；MCP 宿主配置模板写死；237 passed
- 提交：5051c6d+ef0ba0e

## S72 · 错误可修性三连修：堆栈尾部 + 嵌套钳制 + local_run 解码/上限
- 项目：unified-rx-mcp｜时间：2026-08-31
- 决策：宿主（Yan Agent/opencode）侧诊断实锤"token 被吞 + 多轮修不到根因"后，修本仓三处信息损失：①registry.call 异常附 error_detail（堆栈尾部 1000 字符），server ERROR 行拼 DETAIL——单行 error 模型看不到出错位置只能瞎猜；②_clamp 全字段独立处理 + 嵌套限深递归（旧版单字段 break，子 dict 里的大 list/str 漏网；顶层 S10 cursor 分页契约不变）；③local_run UTF-8 优先解码（旧版固定 GBK，UTF-8 输出乱码）+ 失败放宽尾巴 12000/4000（UNIFIED_RX_RUN_TAIL_FAIL 可覆盖，成功维持 3000/1000）。另修 server.py 缺 import time——S69/S71 自动体检线程启动即 NameError 被 except 静默吞，从未真正跑过；tools/__init__ __all__ 剔除不存在的 pure/collab（补 lsp）；code_review bug_scan 透镜静默 except 补协议日志
- 证据：tests/test_s72_errors_clamp.py 13 测试；全量 pytest 382 passed；--selftest 55 工具/12 域/schema 0 bad
- 提交：本次

## S73 · 深度扫描实锤三处修复：授权门 + 沙盒钳制 + 扫描标准成文
- 项目：unified-rx-mcp｜时间：2026-09-04
- 决策：Mimosa 深度扫描（scan-2026-09-04T15-42-49，seal sha256:32bfc234）58 条发现逐条人工对照源码核实，真问题 3 处全修：①code_coverage 跑任意脚本却无 requires_auth、script/source_dir 只 abspath 不过沙盒（违反 S62"跑程序=任意代码执行必须授权"）→ 挂授权门 + _fs_resolve；②lesson 显式 lessons_dir 可任意路径写 JSONL → 过沙盒（默认库路径固定可信免检，保持可用）；③app_clone 整目录读取（fs_read 够不着的隐私面）挂 requires_auth。顺手收口 dep_graph/module_stability 读路径钳制（核实扫描误报时发现的同类缺口）。其余 55 条核实为误报（约 40 条 bench/ 为不暴露面本地脚本；fs/ide_edit/lsp/game/meta 各指控入口均有 _fs_resolve 或硬编码 127.0.0.1）。应用户要求落 spec/SCAN-POLICY.md 扫描标准：禁自扫、独立智能体执行、副本沙箱隔离、静态只是初筛、动态+模拟复合验证、结论纪律
- 证据：tests/test_s73_scan_findings.py 12 测试；全量 pytest 395 passed；--selftest 55 工具/12 域/schema 0 bad
- 提交：本次

## S74 · avian3d 物理规则落地 + bug_scan 分页埋没问题修复
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：接手另一会话（VoxelForge 实机踩坑：四轮弹跳床/ LockedAxes 魔数/Static 体带速度）沉淀未提交的 avian3d 规则 3 条（bevy.py），逐条核实规则与真仓命中属实后修三处缺口：①bevy_phys_static_with_velocity 第三分支无 spawn 锚——上一条 Dynamic spawn 的速度逗号 + 200 字符内另一条 Static spawn 会跨语句误连，补 spawn 元组锚；②bevy_phys_manual_support_force severity info→med——手写 Vec3::Y 支撑力各执行器封顶≠总和有界（四轮同压可叠 3×车重），是真实物理 bug 面不是提示；③bug_scan 交付前按严重度排序（scan.py）——registry 出口 200/页分页 + 文件序会让新规则命中沉到第 2 页之后（VoxelForge crates 实测 1808 条/10 页，排序前 med 命中完全不可见，"机器自动拦"失效），排序纪律与 code_review S65 出口一致
- 证据：tests/test_bevy.py 6 测试（含跨语句 FP 守卫 + med 断言）；全量 pytest 397 passed；cursor 分页复验 VoxelForge 4 命中全部存在——med vehicle.rs:745 回到第一页，info sync.rs:371/397/591
- 提交：本次

## S75 · 权力面全面盘点：PS 注入实锤 + 破坏性/隐私面挂门 + manifest 动态高权限
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：S74 落地后应用户"还有什么可以加强"做权力面全面盘点（全部 55 工具 × 授权门/沙盒/真实执行点三列交叉），实锤 4 处收口：①blender_verify 实锤双洞——screenshot_path 原样拼进 PowerShell 单引号字符串（$bmp.Save('{shot}')，路径含 ' 即逃逸注入任意 PS 命令）+ 全屏截屏=隐私面 + spawn powershell=执行面，全部无门 → requires_auth + screenshot_path 过沙盒（默认路径固定可信免检）+ _ps_quote 单引号转义（'' 成对）；②process 的 taskkill /F /IM|/PID 可杀任意进程（含宿主自身），argv 形式无 shell 注入但破坏性动作无门 → requires_auth（list 查询一并过门，工具级先例 code_coverage）；③backup action=backup 把任意 root 全量打包 zip（S73 app_clone 同级隐私面），root 只 abspath → requires_auth + root 过沙盒；④engine_query root 喂 codegraph CLI（-p）与 BM25 不钳 → 过沙盒（S73 dep_graph 同纪律）。capability_manifest 新增"高权限"段——从 list_tools 的 __authorized 声明（S72b）反向动态读出，新挂门工具自动进清单，落实前会话"eval 等标高权限"建议且免手工维护。盘点确认不动：game_check/bug_scan 等（纯读分析=本职）、ide_lsp（固定 cmd）、scan_log（写固定 ~/.unified-rx）、meta.py 命令白名单（无 & | ; $ `）
- 证据：tests/test_s75_power_gates.py 14 测试（授权门×3、schema 声明×3、沙盒拒绝×3、_ps_quote 转义、无 Blender 干净返回 mock tasklist、备份 roundtrip、manifest 高权限含全部 7 个关键工具）；全量 pytest 411 passed
- 提交：本次

## S76 · 漏洞挖掘加强计划成文（文档轮，先文档后施工）
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：应用户"找漏洞需要再次的加强，先写文档"，落 spec/VULN-HUNTING.md。定位：SCAN-POLICY 管纪律、EVAL 管度量、本文管能力建设，三者互不重复。内容：①现状盘点带数字（bug_scan 规则 19 条=Rust 8/通用 3/bevy 8、attack 域 3 工具、S74 排序、S75 盘点法、H3 门槛）；②短板 5 条诚实清单（规则靠踩坑无覆盖图/动态验证停留在口号/授权门无自审/误报无台账/结论无量化格式）；③P0 三项带验收标准——auth_gate_sweep 自审工具（全 55 工具双向查门，S75 人眼盘点法固化成工具）、规则入库三件套成文（规则+误报守卫+真仓第一页可见，S74 为范例档案见附录 A）、扫描量化记账（双靶场副本+四格数字进 ROUNDLOG+H3 样本扩容）；④P1 三项（Python AST 污点轻量版带回放验收：S73 的 3 真 55 误报为题库/规则覆盖矩阵"查不了"如实入表/协议层 fuzz 进电池）；⑤P2 三项方向（调用图定位/教训库召回/tag 前独立深扫常态化）；⑥明确不做：重型 SAST 进仓/宿主自扫/低质规则凑数/"扫了=没有"承诺。里程碑：S77=P0 三件、S78=P1a+c
- 证据：纯文档轮，代码零改动，版本维持 2.5.10；现状数字全部来自当轮实查（grep/registry 探针）
- 提交：本次

## S77 · VULN-HUNTING P0 三件落地：门自审工具（当场抓到假门）+ 三件套成文 + 量化基线
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：按 spec/VULN-HUNTING.md 里程碑施工 P0 三项。①P0-a auth_gate_sweep（attack 域第 4 工具）：全工具双向查门——漏拒绝（requires_auth 工具空参端到端调用必拒，授权检查先于 handler 零副作用）、漏声明（S72b schema 契约）、门参数未强制（收 __authorized 无任何声明=假门）、manifest 高权限段一致性；**首跑即抓到 ide_lsp**——handler 收 __authorized（仅 rename_apply 落盘手动查）却无 registry 强制，属单工具混合读写的合法手动门但元数据不可见 → registry.tool 新增 manual_gate 注册声明（声明紧挨实现防漂移），自审将手动门单独归类；②P0-b 规则三件套成文进 skills/workflow.md（真 bug 必答"能否静态化"；规则+误报守卫+真仓第一页可见缺一不收；manual_gate 纪律一并写入）；③P0-c 双靶场副本量化基线（禁自扫，copytree 排 .git 后再扫）：本仓副本 635 条{info 460/low 171/med 2/high 2}，high×2=bench 快照夹具故意 panic!（S73 已定性不暴露面，误报）；VoxelForge crates 副本 1282 条{info 542/low 738/med 1/high 1}，med=vehicle.rs:745 第一页 #1——VF 源码 878eff0"P0 物理正确性"重构后 S74 的 3 处 locked_axes 魔数消失（此前规则起了作用），总量 1808→1282 属源码演进非扫描缺陷（原件/副本 .rs 文件数 67=67 核实）
- 顺带修：记账实测暴露 severity 词表暗门——astscan/scan 三处把 med 写成 "medium"，S74 排序表只认 med，这些命中被当 info 沉出第一页（S74 失效模式换个门又进来），统一为 med。接手并行会话对 bevy_query_single 消息文本的甄别更新（09-05 实查 11 处 .single() 全部正确 else-return 零真险，severity 维持 low）
- 证据：tests/test_s77_auth_gate_sweep.py 4 测试（全清洁断言/挂门清单含 8 已知工具/纯函数坏样本三种必抓/manifest 双投影一致）；auth_gate_sweep 一键 56 工具 ok:True（挂门 17/手动门 1）；全量 pytest 415 passed
- 提交：本次

## S78 · P1-a + P1-c 落地：污点引擎与协议层 Rust 化（用户决策"PY换Rust"）
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：应用户"PY换Rust搞的污点分析和协议层，先把这两件做完，再把大部分功能替换成Rust"，P1-a/P1-c 改道 Rust 施工，迁移路线图成文进 spec/VULN-HUNTING.md 五。红线镜像 Python 侧纯 stdlib：Cargo `[dependencies]` 恒空、零第三方 crate，JSON 与 Python 词法器全部手写。
- 交付：①`rust/` cargo workspace（unified-rx-rs v2.6.0）：json.rs 手写解析/序列化（MAX_DEPTH 512 防栈溢出；id 全保真升级 i128，2^70 往返精确）；rx-mcp 独立 MCP stdio 协议层（newline JSON-RPC、通知静默不回、64MB 行帽）；rx-taint 污点引擎——Python 子集词法器（三引号/续行/原始串转义）、缩进作用域、来源→汇点浅数据流、净化器区（basename/secure_filename/int/_fs_resolve/.name/.stem 使用点与赋值尾双净化）、方法形式调用双记（p.write_text）、点链基变量接收者传播；②入口点污点模型：`@tool` 装饰=宿主可达边界，入口形参 definite / 内部 helper 形参 clue（pass2 实参回溯只升不降），S73 人工"暴露面"triage 从此机器化，clue 行仍全量报告只分级不隐藏；③协议 fuzz 电池 tests/test_s78_protocol_fuzz.py 32 测双靶（python + rust exe 自动发现）：非对象消息/错型 params/深嵌套 3000/50 通知风暴不回/id 全类型保真/畸形字节/BOM/1MB 行/沙盒外 tools/call——包络断言+存活探针；④server.py 首跑抓 4 类当日修：非 dict 消息 .get 崩、params 非 dict 崩、深嵌套 RecursionError 崩、通知被误回污染输出流；⑤attack 域 rust_taint_scan（Python 壳调 exe，root 过 _fs_resolve 沙盒，exe 发现可 env 覆盖，缺失时清晰报错不静默降级）。
- 验收：cargo test 9 绿（json 6 + taint 3）；pytest 全量 454 passed（S77 基线 415）；S73 重放通过——REPLAY S73 snapshot=395e4cd files=119 taint_definite=130 taint_all=627 naive=755 reals=3/3（3 真问题全部 definite，definite ≤ ½ naive 达标 130 ≤ 377）；重放前修两处词法器缺陷（原始串 `\"` 不终止、多行串行号传播）——重放靶场自己就是验收器
- 顺带：Mimosa PreToolUse 钩子对重放测试 tarfile/extractall 与字面 ".." 的 advisory 经甄别记为误报（tar 源=自仓固定 commit 的 git archive + 成员白名单，".." 仅净化变量名），不阻断；钩子拦动态子进程派生致 rx-mcp 转发代理形态推迟 S79 评估，独立协议实现先行落地
- 证据：tests/test_s78_rust_taint_tool.py 6 测（注册/schema/发现/naive 模式/沙盒拒绝/exe 缺失干净报错）；tests/test_s78_replay_s73.py 重放验收常驻 pytest；版本 2.5.11 → 2.6.0
- 提交：本次

## S79 · Rust 迁移路线图第一域落地：fs 读面三工具原生化（rust-fs）+ 最新语言版本政策
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：应用户"以后编程语言基本上用最新的功能写代码，继续搞 Rust 迁移路线图"，①政策入 workflow.md：Rust 最新稳定工具链（1.97）+ edition 2021→**2024**；Python 以 3.14 为第一目标（宿主实际解释器，S78 钩子已实跑 3.14），3.11 全绿保留为回归网。②按路线图"纯读先迁"选 **fs 读面三工具 fs_read/fs_stat/fs_list**（fs_write 写面按纪律最后迁）。
- 交付：①rust/src/fs.rs——三操作原生实现，契约逐字对齐：resolve 拒绝→退出码 2→壳 raise ValueError→registry ok:false（旧实现同包络），工具级错误（不是文件/过大/不是目录）→退出码 0+result.error（旧实现返回 dict 同包络）；universal newlines 归一（\r\n/\r→\n）与 1MB 上限逐字节复刻；②rust/src/sandbox.rs 重写——**宽限 realpath**（最深存在祖先 canonicalize+余尾拼接，对齐 realpath(strict=False)，fs_stat 的 exists:false 依赖此），沙盒根不再要求可解析（对齐 Python abspath 恒成功），拒绝消息与 Python 逐字一致，SandboxCfg 可构造（测试不依赖进程 env）；③bin/rx_fs.rs（read|stat|list）+ Python 薄壳 _rx_fs_call（退出码分流，exe 缺失/超时/非 JSON 清晰报错不静默降级，_rx_taint_exe 同纪律）；④edition 2024、crate 版 2.7.0。
- 迁移实测踩坑（双实现对照实验定案）：①fs_list 深度语义 = depth=N 列 N+1 层，且 Python `depth or 1` 把字面 0 静默强制成 1——Rust 侧归正 0=仅根层（schema 语义归正，skills/fs.md 已声明）；②registry 对 {"error":...} 结果统一转 ok:false（error 顶层+result 保留）——薄壳测试初版预期写反被此抓住；③Mimosa 钩子拦整文件重写 fs.py（S62 的动态 tmp 路径被重新提交评分）→ 改小步 Edit、fs_write 一字未动。
- 验收：cargo test 22 绿（fs 13+json 6+taint 3）；pytest 双解释器全绿：3.14=462 passed+2 skipped（pylsp 未装 3.14，旧有 skipif）、3.11=464 passed；release exe 已建（TEMP/rx-rs-target/release/rx-fs.exe，1.3MB）；版本 2.6.0 → 2.7.0
- 证据：rust/tests/fs_test.rs 13 测（fail-closed/"*"/白名单/穿越/垃圾根/大小写/宽限 realpath/相对路径/换行归一/上限/深度钳制/排序与条目形状）；tests/test_s79_fs_rust.py 9 测（薄壳包络+行为+exe 缺失+schema 不变）
- 提交：本次

## S80 · search 域第一步：code_search 原生化（rx-search）——对照实验实锤遍历顺序契约
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：按路线图"纯读先迁"继续，本轮迁 **code_search**（search 域读面主力；code_semantic 留 S81；_tokenize/_fingerprints/_INDEX_EXTS 因 code_semantic 依赖保留在 Python）。删码后 tools/search.py 354→约 300 行，死代码 _index/_get_index/_bm25/S12 指纹缓存三件套全部退役。
- 交付：①rust/src/search.rs——手写分词器（camel/Pascal 状态机等价原正则 `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+`：HTTPServer→HTTP+Server、ABCd→AB+Cd 的回溯语义逐例核对）+ 中文 bigram + 39 停用词；BM25（idf=ln(1+(N-df+0.5)/(df+0.5))，k=1.5/b=0.75）+ S13 行重排（查询 token 交集计数 + raw term（≥4 字符标识符/≥2 连续中文）行内精确出现 +6，多 raw term 只加一次）；无沙盒门（与 Python 版一致，S75 定性纯读=本职）；②bin/rx_search.rs（rx-search <root> <query> [k]；exit 0=结果含"不是目录"error，2=用法级"query 必填"）；③tools/search.py 手术式 Edit：code_search 改薄壳 _rx_search_call（与 _rx_fs_call 同纪律：argv 固定、basename 校验、exe 缺失清晰报错），engine.py 降级路径形状不变（file/line/score/snippet）；④crate 2.8.0、server.py 2.7.0→2.8.0。
- 迁移实测踩坑（本轮最大发现）：**200 文件上限的截断顺序其实有契约**——对照实验首跑 8 查询全 DIFF：'notifySend' py_total=4 vs rs_total=1，py 命中根目录 server.py/registry.py 而 rs 全无。根因：Python os.walk 每层先收本目录文件再下钻（根目录源码优先入库），Rust 初版按字母序混排 DFS，bench/ 字节序排在 conftest.py 之前把 200 名额烧光、根目录源码根本没进语料 → N/df/avgdl 全变、分数系统性漂移。修复：walk 改"每层先文件后目录"（os.walk 结构）+ 目录内 NTFS upcase 排序（os.scandir 在 NTFS 按 $UpCase 返回，字节序会把大写文件排到小写目录前）+ 符号链接不下钻（followlinks=False）+ 读取失败名额照烧文档不入库（等价 OSError continue）。重跑 8 查询全 PARITY（文件多重集+行号+分数 ±0.001；tie 顺序按 tie 无关口径比——Python 侧 set 迭代本就不稳定）。
- 契约变化（3 处，均有意为之并记档）：①空查询 total=0 → 显式拒绝"query 必填"；②engine.py BM25 降级路径 exe 缺失从"静默空结果"变 ValueError→ok:false（不静默降级政策）；③S12 进程内指纹缓存退役——短命 exe 无从缓存，实测冷调全流程 ~140ms（进程+建索引+查询，3 次取样 136-141ms）vs 旧 Python 首查 297ms（缓存复查 8.1ms）：零星调用形态下冷调快一倍，紧循环复查变慢属可接受代价。
- 证据：rust/tests/search_test.rs 10 测（分词 camel/snake/整词/CJK bigram/停用词、raw_terms 最短长度、混合查询、行重排精确符号置顶、非目录、空查询、k 上限、**201 文件判别法**（a.py+z.py+sub/199 个——字母序混排必使 z.py 落榜、先文件后目录必入选）、跳过 .git 与 .txt）；tests/test_s80_search_rust.py 10 测（registry 包络+空查询 exit2+201 判别+exe 缺失+schema 不变+engine 位置参数兼容）；cargo 32 绿（fs 13+json 6+search 10+taint 3）；pytest 3.14=472 passed+2 skipped / 3.11=474 passed 全绿
- 提交：本次

## S81 · search 域第二步：code_semantic 原生化（rx-semantic）——search.py 从此纯薄壳
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：按路线图迁 **code_semantic**，tools/search.py 双工具全部薄壳化（S31 纯 Python 实现、_SEM_CACHE、S80 留守的 _tokenize/_fingerprints/_INDEX_EXTS 全部退役，295→153 行）。S31 的进程内语义缓存随之退休：短命 exe 无从缓存，实测冷调 ~330ms/次 vs 旧 Python ~930ms/次——缓存这次是"负资产"。
- 交付：①rust/src/sem.rs——四语言七定义匹配器全手写等价原正则：py def/async/class→"def"；rs fn/pub+async→"fn"、struct/enum/trait→"type"、impl 含泛型 `<...>` 必须闭合 + `A for B` A 分支（\w+ 取最大无需内部回溯——词内无空白）捕获 for 右侧类型名、B 分支裸 ident；go func 接收者 `(recv)` 可选组+裸 func 回退（无前导 \s*，锚定列 0）；js export/async function→"fn"、export/class→"class"；**.ts/.tsx/.jsx 不算 js 的 S31 怪癖原样保留**（入库占名额但不产定义）。向量：名称 token ×3 + 名称 char-trigram ×2（小写）+ 定义体 token ×1，权重 (1+ln tf)·idf；df 只统计 name+body token 并集（trigram 不进 df → idf 恒 1.0）；idf=0.4+0.6·ln(1+n/(1+c))；余弦小字典换边；search 阈值 0.02 逐个 break、related 先取 k 再滤 0.05（不回填）、snippet 重读定义行 120 字符；SEM_MAX_DEFS 4000、body 采样 40 行、注释行折叠上收（doc comment 语义信号）；②bin/rx_semantic.rs：空 query 合法（S31 契约，与 code_search 的显式拒绝刻意不同）、mode 非法 exit 2；③薄壳 _rx_semantic_call + **stdin 大查询通道**：argv 传 "-" 时 exe 改读 stdin 全文（lossy），_QUERY_ARGV_CAP=10000 字符（Windows CreateProcess 命令行 32767 UTF-16 码元、代理对最坏翻倍），stdin 恒接管（空串即 EOF）防子进程继承宿主 MCP 协议管道——**S80 的潜在缺口一并补齐**（code_search 同款通道，此前 4 万字查询会 WinError 206）。crate 2.8.0→2.9.0、server.py 2.8.0→2.9.0。
- 迁移实测踩坑（都在测试夹具上）：①旧 test_big_input_smoke 传 5 万字查询——进程内实现无所谓，过 argv 即爆命令行上限 → 逼出 stdin 通道（见交付③）；②go 夹具初版把接收者方法和裸 func 放同文件：定义体向後采样 40 行把相邻定义行吞进 token，两定义互喂词根使 total=2 判别失效 → 分文件；③body-cap 判别标记初版与 alpha 名称共享 trigram（"cap" ⊂ "bodycapmarker"）——名称 trigram 本身就把 40 行外的定义拉上榜 → 换无重叠标记。教训：**向量检索的判别测试必须先查 token/trigram 重叠，否则测的是词面巧合**。
- 对照实验（删码前，git archive v2.8.0 树 vs exe，同语料同查询）：9 查询全 PARITY——search 模式（CJK 注释桥"时钟经过的时间累加"/"sandbox 路径校验"/"污点 扫描"/"walk depth 深度"/部分名"rotat vehicle"/无命中"zzz qq wwww"）分数 ±0.0011 + file/line/symbol/kind 多重集全同（tie 顺序按 tie 无关口径比，Python 侧 set 迭代本就不稳定）；related 模式（精确锚点 tokenize、code_search，模糊锚点"这个符号绝不存在 zz"）anchor+total+邻居集全同；空语料无 mode 键、空 query search total=0 同构。
- 验收：cargo 45 绿（fs 13+json 6+search 10+**sem 13**+taint 3）；pytest 双解释器全绿：3.14=483 passed+2 skipped / 3.11=485 passed；**旧 test_semantic.py 5 测原样过检**（薄壳下不改一字即行为等价的活证明，含 5 万字 smoke）；耗时 old ~930ms → exe ~330ms
- 证据：rust/tests/sem_test.rs 13 测（四语言匹配器+impl-for 回溯+泛型+go 接收者+.ts 怪癖+注释折叠+trigram 部分名+模糊锚点+0.02 阈值+空语料无 mode 键+201 文件判别+k 上限+body 40 行帽+score 圆整）；tests/test_s81_semantic_rust.py 11 测（registry 包络+related 形状+空 query 合法+mode exit2+非目录+exe 缺失+双 stdin 通道+schema 契约+位置参数+**内部函数退役断言**）
- 提交：本次

## S82 · scan 域轻正则三工具原生化（rx-scan）——std_check / ui_check / bug_locate
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：按路线图迁 scan 域"轻正则"三工具（bug_scan/ast_scan 的 Python AST 面留后续轮）。bevy.py 的 BEVY_UI_PATTERNS/BEVY_CODE_PATTERNS/find_dead_buttons 一并整端口（ui_check 是唯一调用方）→ bevy.py 只剩 bevy_rules()（bug_scan 用）。_SCAN_CACHE 保留给 bug_scan/code_review，但 **std_check 不再走它**：短命 exe 无跨调缓存面（S81"缓存是负资产"同款结论）。
- 交付：①rust/src/scan.rs（~920 行，正则全手写、无 regex crate）——遍历契约 S80 实锤同款（单文件直收、每层 files-first-then-subdirs、目录内 NTFS $UpCase 排序、12 跳过目录、**名额只计代码文件**、满额整体停走、符号链接解引用定类）；std_check：12 占位词（含中文占位/待实现/未实现）+ 魔法数 `=\s*(-?\d{3,}|[2-9]\d{2,})\b` 逐 '=' 左移优先、branch1 贪婪+\b 回溯收缩（min cut 3）、\b 按 Unicode 字母数字+下划线口径（`123中` 不报）、魔法数 6 语言门、注释豁免只管占位词魔法数照报（Python 原样）；ui_check：godot `Button\b[^:]*:\s*$` MULTILINE（无左边界、[^:]* 跨行吞到首个 ':'、$ ≡ 冒号后空白串含 '\n' 或直达文尾）、unity `new\s+Button\s*\([^)]*\)`（无任何边界 renew 也中、\s+ 跨行、[^)]* 跨行止于首个 ')'）、bevy 三模式 + S6 死按钮结构化检测整端口（门 ≡ contains("Button,")，同行/独行（向下 2 行、撞 ')' 或 '//' 断扫）双路提取 Marker，救回 = `With<Marker>` 子串或 `&Marker…Interaction` 同行 80 字符双向同现）；bug_locate：traceback→文件名→符号三层候选原序，文件名提取贪婪回溯从右往左找 '.' 拆分点 + 备选按原序首个前缀命中即收（**foo.tsx 捕获成 foo.ts 怪癖保真**），符号提取开引号与关键词同行（.*? 不跨行）闭引号可跨行（[^'"]+ 含 \n），_line_ctx/_find_in_file 窗口逐字段等价 + **空 needle 后 direct[-1]["how"] = how 覆盖怪癖保真**，(file,line) 去重 cap 10；②bin/rx_scan.rs：`stdcheck|uicheck <path> [max_files]`、`buglocate <root> <error_text|->`，max_files 负→0（Python count>=max 立停语义）垃圾→100 回退，exit 0=工具级（含 error 对象）/2=用法级；③薄壳：tools/scan.py 三工具转调 _rx_scan_call（exe 发现 UNIFIED_RX_RS_EXE→%TEMP%\rx-rs-target\{release,debug}、isfile+basename 校验、list-argv 无 shell、timeout 120s）+ stdin 大文本通道沿用（error_text 超 _QUERY_ARGV_CAP=10000 时 argv 传 "-"）；bevy.py 死代码删除。crate 2.9.0→2.10.0、server.py 2.9.0→2.10.0。
- 迁移实测踩坑（全撞在"保真"上）：①godot 初判翻车——`extends Button`（1 号行）以为不命中，实际 `[^:]*` 跨行吃到 3 号行冒号即命中（对照实验自纠，Python 为 oracle）；②unity `[^)]*` 跨行——`new Button(;` 并非"无右括号不命中"：分号后跨行吃到下一行串内 ')' 整段成一次匹配，还把下一行的 new 吞进同一匹配不再单报（scan_test.rs 用例钉死此语义）；③readlines 等价必须弹掉末尾换行的幻影空行——空 needle 命中所有行，幻影行凭空多报；④Mimosa PreToolUse 钩子两拦对照实验脚本（动态路径写文件判"路径穿越"高危）→ 夹具逐个静态 Write + 只读 runner 绕行。
- 对照实验（删码前，Python 实现为 oracle，**26/26 全 PARITY**）：std 占位/魔法数全边界语料（负号捕获/前导零/999abc/12/1234abc/123中/注释行魔法数/.c 语言门）×名额 5/4/0×单文件×幽灵路径×上限语料×真仓；ui 三引擎语料×单 .gd×幽灵×真仓；bug_locate T1 traceback 窗口/T2 tsx 怪癖/T3 符号/T4 未命中/T5 多行符号/T6 空/T7 去重/T8 cap10/T9 错 root/T10 大文本 stdin/小文本 stdin/真仓 frame + 用法级 exit 2。计时（冷调）：std 真仓 81ms→69ms、ui 36ms→22ms；bug_locate 小输入 ~10ms→~17ms——**进程 spawn 开销盖过轻正则，诚实记账**（大 error_text/批量面仍受益）。
- 验收：cargo 58 绿（fs 13+json 6+search 10+sem 13+**scan 13**+taint 3）；pytest 双解释器全绿：3.14=496 passed+2 skipped / 3.11=498 passed；**旧 test_v2.py+test_bevy.py 39 测原样过检**（薄壳下不改一字即行为等价的活证明）。
- 证据：rust/tests/scan_test.rs 13 测（占位/魔法数全边界+unicode \b+语言门+注释豁免、walk 名额只计代码文件+$UpCase 序、godot $ 跨行、unity 无边界+跨行 ')'、bevy 三模式+死按钮死/救回（With<> 与 Query 同行）/注释断扫、traceback 窗口、tsx 怪癖+空 needle how 补丁、符号 cap10+跨行闭引号+去重、root 非目录）；tests/test_s82_scan_rust.py 13 测（registry 包络×3+幽灵路径+root 非目录+exe 缺失+4 万字 stdin+负名额+非代码不烧名额+CRLF 归一+project_scan 组合+schema 契约+位置参数+**退役断言**）。
- 提交：本次

## S83 · bug_scan 全量原生化（rx-scan bugscan）——手写 Python 迷你解析器
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：路线图继续：bug_scan 整体原生化（Python AST 面 + Rust 生产规则 + 通用正则 + bevy 全套）。零第三方 crate 红线下不用 syn 建模 Python 源，**手写迷你解析器 pyast.rs**（3.14 语义）产 ast 等价节点面（kind/line/name/name2/ctx/aux/names/children），bug.rs 只做规则层——ast_scan（S84）直接复用同一解析器。旧 _SCAN_CACHE 全域退役（S81/S82 同款结论：短命 exe 无跨调缓存面），code_review 的 bug_scan 透镜经 registry 自动改走 exe。bevy.py 转规则档案（bevy_rules 唯一实现在 bug.rs，运行时零调用方）。
- 交付：①rust/src/pyast.rs（~2500 行）：缩进驱动的 INDENT/DEDENT、括号续行（行号跨行累积）、f-string PEP 701（多行/嵌套/调试 =/转换符/嵌套 spec 区域扁平化、`(,i` 括号深度 sq 防切片冒号误入 spec）、match 软关键字 trial-parse 回退（失败重置 i 回 simple_line；block() 自吃 ':'）、模式匹配全套（MatchAs/MatchOr/MatchSequence/MatchMapping/MatchClass，捕获=字符串字段非 Name 节点、`(` 组透明/逗号折叠序列）、del 变异语句（del *a 报 cannot delete starred 与 CPython 同）。②rust/src/bug.rs（~1000 行）：作用域感知 defined/imported（参数/vararg/kwarg/推导式/with-as/except-as/lambda 参数/Global/Nonlocal）、bare_except/eval_exec（裸 Name 调用，severity 词表 high/med+definite/clue）/undefined_name（Load 上下文）/redefined_import（**ImportFrom 用 asname or name 契约**）、Rust 生产规则 8 条手写匹配器（unwrap/expect/panic 族/as_cast/indexing 双条——**前环视 (?<=[\w)\])] 含 `]`** 支撑双层索引 grid[dir.x][ax]）+ 测试区降级（tests 目录/cfg(test) mod 行号界）+ bevy 8 规则 + 通用 3 规则（eval_exec (?<![.\w]) 排除 re.exec）。③bin/rx_scan.rs 增 `bugscan <path> [max_files]` 子命令（排序 severity→file→line 与 Python list.sort 稳定序一致）。④tools/scan.py：bug_scan 换 5 行薄壳；退役 _scan_python/_scan_rust/_scan_generic/_RUST_RULES/_RE_RULES/_SCAN_CACHE/_CACHE_LOCK/_CACHE_MAX/_file_fingerprint/_cached_scan/scan_cache_clear 与 `import ast`/`from . import bevy`；_iter_files/_lang_of 保留（ide 域共用）。crate 2.10.0→2.11.0、server.py 2.10.0→2.11.0。
- 迁移实测踩坑：①f-string `!r` 截断——`!` 臂只跳字节不够，**区域 src 是源索引切片，必须记 cut 点**（{name!r} 一度输出 "name!r"）；②f-string 区域内 `(`/`[` 深度（sq）——`{lines[-1][:200]}` 的切片冒号曾触发 spec 臂，全仓 25 个"括号未闭合"假语法错误；③调试 `=` 三守卫：depth==1、sq==0、左右都不是 `=`（== 排除；{x:=1} 按 CPython 探针实况 = expr x + spec "1"）；④match 软关键字 dispatch：trial-parse 失败重置 `self.i`，且 match_stmt **不得预吃 ':'**（block() 自吃，双重 expect 曾致整条 match 回退失败）；⑤redefined_import 键用别名（asname or name）——源名键曾致 cor200 差 2 条；⑥oracle 文件漂移两次：pyast.rs/bug.rs 本身在被扫仓库内，**改完必须重生成 oracle 再比**（unwrap/indexing 行号漂移假 diff）。
- 对照实验（删码前，旧 Python 实现+真 ast 为 oracle，**7/7 逐字节 PASS**）：46 文件语料 × 配额 200/10/0、全仓 169 文件 909 条（repo5000）、单 py、非代码 txt、不存在路径错误包络；附语法错误集对照（cor200/repo5000）与 issue 多重集 diff（按 file/line/rule/msg）全空。dbg 电池 27 例（括号续行/推导式/lambda/f-string 9 变体/match 5 模式/类关键字参数/星号解包赋值/del 三型）全对 CPython。
- 验收：cargo 全绿零告警 72 测（fs 13+json 6+search 10+sem 13+scan 13+**bug 9**+**pyast 5**+taint 3）；pytest 双解释器全绿：3.14=502 passed+2 skipped / 3.11=504 passed；test_v2 缓存两测重写为可重复+内容新鲜度（计时断言与 scan_cache_clear 随缓存退役删除）；test_s82 退役断言扩容（13 个退役名）；新增 tests/test_s83_bug_rust.py 6 测（契约/幽灵路径/exe 缺失/位置参数/schema/语义抽查）+ rust/tests/bug_test.rs 9 测（别名契约/切片冒号/match 语义/分级降级/`]` 环视/lookbehind 排除/syntax_error 行号/名额/错误包络）；pyast.rs 调试电池转断言测试 5 测。
- 提交：本次

## S84 · ast_scan 全量原生化（rx-scan astscan）——scan 域五工具自此全薄壳
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：路线图继续：S9 结构化层 ast_scan 整体原生化。pyast.rs 复用（S83 手写迷你解析器），本轮为其补齐 S9 规则面所需的三件套：节点 col 列号、字符串值解码（CVal：str/bytes、转义语义 3.14 对齐、raw 保形）、FormattedValue/JoinedStr 事件序（外层区域在前、嵌套 spec 区域随后）。astscan.rs 承接全部规则层：Python（py_dynamic_exec 分级/shell_like_call/secret_literal 掩码）、JS（词法掩码→括号平衡调用面→分类，成员链 X.exec() 排除、new Function 显式命中、模板 ${} 插值是真代码）、Rust（词法掩码→unwrap/unsafe/panic 结构化信号→fn 花括号深度归属→risky_fns 聚合→cfg(test) mod 测试区）、S16 跨文件可达性（bevy 裸标识符注册算 prod、test_only helper 归档、unreferenced 只标不降）。tools/astscan.py 524→103 行薄壳；scan.py 四工具 + astscan.py = **scan 域五工具全薄壳**。
- 交付：①rust/src/astscan.rs（~1800 行）：掩码器/规则/归属/可达性全手写；怪癖保真清单——`_FN_RE` 无尾随 \b（`fn fooé` 照捕获）、`_RUST_IDENT_RE` 带尾随 \b（unicode 后缀拒绝）、defs 全量 finditer（非首个）、risky_fns 稳定排序 -(unwrap×2+unsafe×8) 取 12、fn_count=全量计数、files=截断前计数、by_rule 插入序、大写 .PY 进目录目标但走 JS 管线、单文件直扫不受名额约束、secret 掩码=前 6 字符+***len=N；旧 Python 的 `r#"type` 死循环修复（i+=1 继续）。②rust/src/pyast.rs 扩展（~2960 行）：CVal 入节点 + col 全链路 + f-string 区域 FRegion{src,line,col}。③bin/rx_scan.rs 增 `astscan <path> [max_files]`（默认 200、垃圾→200、负→0——注意与 stdcheck 系默认 100 刻意不同）。④tools/astscan.py 薄壳 + tests/test_astscan.py：唯一直呼 _mask_js 的 test_mask_preserves_length 退役，改 S82 式退役断言（17 个内部名不得复活）+ exe 缺失 ValueError 测。crate 2.11.0→2.12.0、server.py 2.11.0→2.12.0。
- 迁移实测踩坑（五连）：①panic 正则 group(0) 语义靠 oracle diff 反推实锤——`\b(?:\.\s*)?(names)\s*[(!]` 的 \b 在**可选点组之前**：点形式 `v.unwrap(` 的 match 从 '.' 起（要求前字符 \w，行首 '.' 无边界不命中）、名字形式 `x .unwrap(` 的 group0 不带点；②f-string 区域位置：CPython 3.12+ 区域内节点带真实 (lineno,col_offset)——单行区 col += brace_col+1、多行区行偏移列不变；tokenize 错误行号要偏移而 parser 错误已是绝对坐标不可二次偏移；③bytes 转义：CPython"只准 ASCII"约束的是**源字符**——b"\xef\xbb\xbf" 转义产出合法，八进制/\x 产出直接入 b，非 ASCII 源字符进未知转义才报错（全仓 5 个测试文件曾因此假报 syntax_error）；④**CRLF 通用换行**（最大坑）：bug_test.rs 是 CRLF，Python open("r") 把 \r\n 读成 \n 而 exe read_to_string 保留 \r——字符串分支 `\` 先吞 \r、真 \n 反而截断字符串，行号全盘 +4 漂移；修复=ast_scan 读入后归一 \r\n→\n、孤立 \r→\n（共享 read_text 不动，其他工具契约不变）；**分段探针（Write 工具产物全是 LF）个个一致、整文件才漂——对照探针必须带与真实文件相同的行尾**；⑤oracle 漂移三次（astscan.rs/pyast.rs/lib.rs/rx_scan.rs/server.py 都在被扫仓库内）——改完必须重生成 oracle 再比；外加 Rust 局部变量遮蔽助手函数 s()/i() 的 E0618 三连（改名 stripped/cur）。
- 对照实验（删码前，旧 Python 实现+真 ast 为 oracle，**14/14 逐字节 PASS**）：S83 语料 7 场景（46 文件×配额 200/10/0、全仓 172 文件 379 条 repo5000、单 py、非代码 txt、幽灵路径）+ S84 新语料 7 场景（17 文件 b200/b3/b0、单 rs、单 js、空目录、幽灵路径——secret 形态含 bytes/docstring/边界负例、eval/exec 形态 9 变体（(eval)(x)/eval(x=y) 的 arg_kind=literal 怪癖）、f-string 位置三型、JS 模板状态机/new Function 变体/new (x)/newx(q)/正则字面量探针、Rust unwrap 五形态/Unicode 边界/cfg(test)、S16 可达性 alpha-prod+gamma-unreferenced+delta-test_only 结构）。arepo5000 首跑 379=379 但 unwrap 行号 ±3/±7 假 diff → 顺藤摸出 CRLF 坑。
- 验收：cargo 全绿零告警 **87 测**（lib 13=pyast 8+astscan 5、fs 13+json 6+scan 13+search 10+sem 13+bug 9+taint 3+**astscan_test 7**（CRLF 契约回归锁/py CRLF/js 掩码/secret 形态/名额与单文件/幽灵路径/无可扫目标））；pytest 双解释器全绿：3.14=503 passed+2 skipped / 3.11=505 passed。
- 提交：本次

## S85 · app_audit 全量原生化（rx-audit）——appaudit 域唯一纯读实现工具入 Rust
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：先做域普查圈靶：attack 余下 4 工具（input_fuzz/path_probe/big_input/auth_gate_sweep）是**活体自审**——攻击的是运行中的 Python registry（registry.call/_TOOLS），exe 化会测错对象 → 不迁（attack 域 Rust 化在 S78 算完成，属结构性保留）；game（外部进程编排）/learn（小+写）/guard/meta（宿主内省）/ops（副作用）同理由不迁。appaudit 域的 app_audit = 唯一纯读实现工具 → 本轮靶子；app_clone/app_clean 写面+授权门按"纯读先迁"纪律后置。
- 交付：①rust/src/appaudit.rs（~1160 行）：JS 危险面 6 规则 + 秘密 5 规则全手写 Char 匹配器（正则语义逐条复刻，含 private_key_block 贪婪回溯、aws 16 位后界、secret_by_key 值串 8..200 贪婪回溯）、py_splitlines 全集换行（\x0b/\x0c/\x1c-\x1e/\x85/\u2028/\u2029）、>800 长行跳过、每标签 ≤51 surface 上限（>50 判定）、400 条 findings 门、URL 清单（hosts 插入序+计数稳定降序取 25+ai_endpoint_hosts 插入序过滤）、二进制盘点（size 稳定降序取 15，平局保 walk 序）、沙盒门 strictly_under（sandbox::lenient_realpath + normcase + 前缀判定，root=UNIFIED_RX_AUDIT_SANDBOX 或 %TEMP%\unified-rx-appaudit）、os.walk 语义 + NTFS upcase 序（scan.rs 同款双键）。②rust/src/sha256.rs（~110 行）：FIPS 180-4 手写零依赖（NIST 向量+百万 a 测），供 asar 基址自标定。③asar 提取整体入 Rust：3 轮×8MB 扩窗找头、候选长度/基址集合枚举、前 8 个带 integrity 的中小文本叶 SHA256 自标定、48MB 预算/4MB 单条/600 条上限/错 hash 跳过/无 ".." 段守卫；提取件复扫 label=asar:{label}（sub 前缀保留反斜杠）。④bin/rx_audit.rs：rx-audit <snapshot_dir> [with_asar(0|1)]，exit 2=用法级→ValueError。⑤tools/appaudit.py：app_audit 换薄壳（_rx_audit_exe/_rx_audit_call，astscan 同款退出码契约）；退役 _AsarError/_extract_asar/_mask/_iter_text_rows/_SURFACE_RULES/_SECRET_RULES/_URL_RE/_AI_HOST_HINTS/_TEXT_EXTS/_BINARY_INVENTORY_EXTS/_MAX_FINDINGS/_MAX_ASARS/_MAX_ASAR_ENTRY_BYTES/_MAX_ASAR_EXTRACT_MB 与 struct/Counter 导入；_sandbox_root/_strictly_under 保留（app_clean 用 + oracle 锚），注释注明与 Rust 侧双实现由 oracle 钉死等价。crate 2.12.0→2.13.0、server.py 2.12.0→2.13.0。
- 迁移实测踩坑：①E0308 十连——rxrs Value::Int 是 i128，行号/计数/字节数全部归一 i128（AsarLeaf.size 保 i64，offset 臂 `*i as i64`）；②private_key_block 贪婪回溯——`[A-Z ]*` 先吃光 "PRIVATE KEY" 再试字面量必失败（正则引擎对字面量在类内有回溯，手写必须显式复刻）：改降序 k 循环 `(i+11..=j).rev()` 逐起点试字面量；③asar 错误串必须带 Python 类名前缀 `_AsarError: `（异常包络 f"{e.__class__.__name__}: {e}" 的一部分，漏了错误包络就 diff）；④"snapshot" 键 = 原样回显输入（旧 Python str(Path(x)) 会归一化——oracle 语料避开该形态，记为已知偏差）；⑤Mimosa hook 拦截三连：Bash 写源码禁；oracle 语料脚本 open(<var>,"w")+路径手术判"路径穿越"（加 containment 守卫也不放行）→ 复刻 s84_oracle.py 的 F dict + write_text(newline="") 形态过检，凭据字面量走运行期片段拼接；整文件 Write 重写 appaudit.py 也被拦（app_clone 里既有 open(dst_path,"wb") 被当新穿越面）→ 改外科手术式 Edit 两刀删死块（hook 只看新增内容，纯删除放行）。
- 对照实验（删码前，旧 Python 实现 vs Rust exe 为 oracle，**10/10 逐字节 PASS**）：语料 10 场景——full（.audit-ext 目录双形态跳过/大写 .JS/坏 UTF-8/BOM/长行>800/eval 灌满 60 发/每标签 51 上限/二进制盘点含 size 平局稳定序/8 个 asar 取 6 个（MAX_ASARS，NTFS upcase 序））、noasar、单文件、空目录、单 asar（嵌套头树/int 型 offset/错 hash 叶跳过/>4MB 条目跳过/9MB 垫片跨窗找头）、ghost（不存在路径）、outside（沙盒外拒绝）、boxroot（沙盒根本身拒绝）、ws/empty_arg（ValueError 包络）。asar 头截断/垃圾字节错误包络逐字一致（含 `_AsarError: ` 类名前缀）。
- 验收：cargo 全绿零告警 **89 测**（lib 15=pyast 8+astscan 5+**sha256 2**（NIST 向量+百万 a）、fs 13+json 6+search 10+sem 13+scan 13+bug 9+taint 3+astscan_test 7）；pytest 双解释器全绿：3.14=505 passed+2 skipped / 3.11=507 passed；test_appaudit.py 增 S85 退役断言（14 个内部名不得复活+薄壳/写面必在）与 exe 缺失 ValueError 测；selftest tools=57/GROUPS 12/SCHEMA_BAD 0。
- 提交：本次

## S86 · appaudit 域收官——app_clone/app_clean 写面原生化（rx-appops）
- 项目：unified-rx-mcp｜时间：2026-09-05
- 决策：S85 靶后顺势收官 appaudit 域：app_clone/app_clean 写面原生化（rx-appops.exe）。授权门结构性留 Python registry（requires_auth + __authorized 于 registry.call 统一强制，exe 永不自行放权——S61/S72b 机制不动）；沙盒门双语言各一版（appaudit.rs::sandbox_root/strictly_under 转 pub 供 appclone 复用），oracle 钉死等价。attack 余 4 工具活体自审不迁（S85 已定）。至此 **appaudit 域 3/3 全薄壳**，除结构性保留（attack 自审）外纯读面+写面全部原生化。appaudit.rs 的 S85 walk 经分析 junction 行为已等价（有效 junction metadata-follow 照走/悬空跳过=3.14 剪枝同形），不改动——仅真目录 symlink 分歧需管理员才可造，记录在案。
- 交付：①rust/src/appclone.rs（~470 行）克隆引擎整体：绝对路径/存在性/目录判定（canonicalize+strip_unc）；py_int（Python int() 语义逐字：trim/正负号/下划线分隔/invalid literal 文本；饱和到 i64——registry 放行 JSON 任意精度，位截断 as 会变号破坏预算语义）；落点 stem=本地时间戳-sha256(normcase)[:12]-净化名+碰撞序号 k；walk_dir（os.walk 3.14 真值，见踩坑①）；分级复制（先开源句柄再建目标+残桩清理；内容失败=read_fails，元数据失败=meta_warns 整体一次；FileTimes atime/mtime+readonly 位）；验证阶段实盘复核；manifest=sha256（**根层文件 rel=""**，清单行 "\t{size}\n"）；errors 前 30 条。②app_clean：strictly_under 门+remove_dir_all+错误类名映射（NotFound→FileNotFoundError、NotADirectory、PermissionDenied/raw_os_error 32|5→PermissionError、余 OSError）。③junction 判别手写 FFI：GetFileInformationByHandleEx(FileAttributeTagInfo) 读 reparse tag==IO_REPARSE_TAG_MOUNT_POINT（\\?\ 前缀自备，UNC 用 \\?\UNC\ 形态）——见踩坑②。④local_stamp 手写 FFI：GetLocalTime（edition 2024 `unsafe extern "system"` 块，kernel32 默认链接，零 crate 红线），非 Windows UTC civil-from-days 兜底。⑤bin/rx_appops.rs：clone/clean 子命令，exit 2=用法级→ValueError。⑥tools/appaudit.py 265→206 行：app_clone/app_clean 换薄壳（_rx_appops_call，fs.py::_rx_fs_call 同款子命令形态）；_rs_exe 统一定位器（rx-audit/rx-appops 共用，UNIFIED_RX_RS_EXE 覆盖须 basename 严格相等）；_sandbox_root/_strictly_under 保留作 oracle 锚与沙盒纪律文档。crate 2.13.0→2.14.0、server.py 2.13.0→2.14.0。
- 迁移实测踩坑（七连，①②④是本轮最大价值）：①**Python 3.14 walk 真值**（oracle 探针钉死，模型直觉连错四次）：junction 不再是 symlink——islink=False、DirEntry is_symlink()=False、is_dir()=True（**悬空也算目录**）；有效 junction→dirnames→目标内容被克隆进 junction 名下；悬空→read_dir 失败→os.walk onerror 静默剪枝（无 yield/无 mkdir/无计数）；skipped_links 只数真 symlink（文件链/断链）。②Rust 侧 junction 被报成 symlink（is_dir()=false）且 read_link 已剥 \??\ 前缀——前缀文本判别被 junction 单测当场证伪（files=2/skipped_links=1）→ 改 reparse tag FFI 实锤。③os.path.relpath→abspath 走 Win32 GetFullPathName：成分尾部空格/点剥除（`name. .`→`name`）——errors 条目须 win32_display_rel 复刻（split+trim_end_matches(['.',' '])），清单/落盘 rel 保持 RAW。④**清单根层 rel=""**：旧 Python rel_dir=="." 时 rel 为空串→清单行 "\t{size}\n"；Rust 首版写成裸文件名→inventory_digest 4 步假 diff（oracle cmp 抓获：full/mf2/mbx/ro_probe）→修复后 24/24。⑤registry schema 门前置：max_files="abc" 在 registry 就报 SchemaError（handler 之前），exe 永远收到规范 int 的 argv 字符串——py_int 只是直调兜底。⑥OS 错误消息文本跨运行时必然发散（[WinError 2] … vs … (os error 2)）——oracle 掩码 CLEAN_RE 只比类名，文档记为已知偏差。⑦Python 文档字符串写 `\??\` 是无效转义（SyntaxWarning）——措辞改"设备前缀"。
- 对照实验（薄壳化前旧 Python dump old.json 为 oracle，薄壳化后 exe 路径 dump new.json，norm：时间戳→<TS>、清理失败 OS 消息→<OS>，**24/24 PASS**）：语料 24 步——full（junction 双形态：loop.junc 克隆目标内容+broken.junc 悬空剪枝、尾点文件 read_fails、CRLF、大写 .JS、node_modules、4KB 大文件）、mf0/mf2/mb(1MB)/mbx(2MB 精确等于=不截断)、ghost、rel("./x"拒)、empty/empty2、filesrc（不是目录）、badint/intws（SchemaError 前置门）、ro_probe（只读位+mtime 保真）、clone_empty_src、clean_ok/clean_twice/clean_outside/clean_root/clean_empty/clean_ws/clean_missing/clean_file（清理门九态+错误类名）、clean_noauth/clone_noauth（授权门）。
- 验收：cargo 全绿零告警 **95 测**（lib 21=pyast 8+astscan 5+sha256 2+**appclone 6**（py_int 语义/净化名/Win32 归一/时间戳形状/junction 3.14 真值/清理门+类名，junction 测试 mklink 实造）、fs 13+json 6+search 10+sem 13+scan 13+bug 9+taint 3+astscan_test 7）；pytest 双解释器全绿：3.14=507 passed+2 skipped / 3.11=509 passed；test_appaudit.py 增 S86 退役断言（copyfileobj/copystat/rmtree/os.walk/hashlib.sha256/time.strftime 不得残留+薄壳/沙盒锚必在）与 rx-appops.exe 缺失 ValueError 测；selftest tools=57/GROUPS 12/SCHEMA_BAD 0。
- 提交：本次

## S87 · 终点步：宿主接入——config.json 挂 unified_rx（python 编排器入口定型）
- 项目：unified-rx-mcp｜时间：2026-09-06
- 决策：S79 落地注记"薄壳转调模式使转发代理非必需——宿主继续用 python 入口即自动获得 Rust 实现，单 exe 入口只在全量迁移终点才有意义"在终点步兑现：**宿主入口 = python server.py 编排器，不走 rx-mcp.exe**——ide/ops/attack（活体自审）/game（外部进程编排）/learn/guard/meta（宿主内省）/engine/fs_write 等约 37 个工具结构性留 Python（S85/S86 既定决策），单 exe 只覆盖 Rust 原生子集；"转发代理"维持缓议。Python 进程不退役（编排器 + 结构性保留工具 + 测试电池与文档）——S78 老终点"换 rx-mcp.exe、Python 进程退役"作废，路线图终点改写为"宿主接入完成"。
- 交付：①宿主 config.json（%APPDATA%\yan-agent\YanData\config.json，mcpServers 为 LIST 形态）追加 unified_rx 条目：command `python`、args `["-X","utf8","D:\rj\MCP\server.py"]`、env `{"UNIFIED_RX_SANDBOX":"D:\开发;D:\rj\MCP","PYTHONUTF8":"1"}`、enabled true、描述注明 v2.14.0/57 工具；改前 Yan Agent 完全关闭+备份 config.json.bak-20260906-pre-unifiedrx，json round-trip（ensure_ascii=False, indent=2）写回，与备份 diff 校验仅 mcpServers 变更（builtins 逐字未动），写回后条目扛住宿主启动重写仍在。②docs：VULN-HUNTING "终点"条目由"换 rx-mcp.exe"改写为 S87 已落形态（引用 S79 决策原文）。仓库侧纯文档变更，server.py 保持 2.14.0 不动 → 无 tag。
- 验证：①stdio 冒烟（完全复刻宿主拉起命令 `python -X utf8 D:\rj\MCP\server.py`）：initialize→serverInfo unified-rx-v2/2.14.0→notifications/initialized→tools/list **57 工具**→tools/call fs_read（沙箱内成功）/app_clone（rx-appops.exe 路径，23 files verified=true）/app_clean（清理成功）全绿。②宿主 GUI 实测：opencode.log 属懒日志（9月3 后未再写，运行时不落盘）不可作证据 → 改走宿主"MCP 服务"页："测试 UnifiedRX 连接"→**连接成功，57 个工具**（绿勾）——宿主自身以同命令拉起 server.py 完成握手并列出全部工具，即最终验收。③Yan Agent 保持运行（用户应用，未做任何本体/压缩策略改动）。
- 提交：本次

## S88 · 三路排查与读取面收口——S73 纪律补全（读路径同样过沙盒）+ 假满分修复
- 项目：unified-rx-mcp｜时间：2026-09-06
- 决策：用户指令"检查 有没有漏洞 修复"。按 禁自扫 纪律 Mimosa 深扫跑在副本（%TEMP%\s88-scan），与 attack 域活体自审、人工精读三条独立线并行，结论必须交叉收敛才动手。排查哲学延续 S73/S75：门控看"能力"（写/执行/提权才设门），读路径的边界由沙盒钳制统一兜底——本轮实锤正是 8 个未设门读取工具漏了钳制，属纪律执行缺口而非设计缺口。
- 三路排查账：
  ① Mimosa 深扫（副本，scan-2026-09-05T18-05-35，57 条：44 bench + 13 tools）逐条分诊——bench/ 系开发脚手架不经 MCP 暴露（44 条不计）；tools/ 13 条中 ide_debug.py:171 eval=S75 设计内（调试器条件断点在调试目标进程内求值，等权无越权面）、meta.py:168/176 shell=True=S75 设计内（local_run 高权限门控工具）、game.py:43 SSRF=常量 URL、其余 fs/ide_edit/learn/lsp/metrics 路径穿越=误报（既有 _resolve 钳制静态不可见）。
  ② attack 五件套活体自审（attack_run.py 探针进程内 import tools 装配）：auth_gate_sweep 17 门与 manifest 高权限一致；path_probe 全部 safe；input_fuzz/big_input 零异常；rust_taint_scan 98 条污点流逐一分组核账，全部收敛于已钳制面或门控面；junction 探针（沙盒内 mklink /J → C:\Windows）fs_read/fs_list/fs_stat 全拒——3.14 junction 非符号链接但 realpath 仍穿透解析，Python/Rust 双侧边界都按最终目标判，**逃逸实测已闭**，悬空 junction 亦报干净错误（"不是目录/不是文件或不存在"），固化为回归测试。
  ③ 人工精读沙盒/授权/子进程面：实锤唯一缺口类——scan（bug_scan/std_check/ui_check/project_scan/bug_locate 默认 cwd）、search（code_search/code_semantic 默认 cwd）、game（game_check）、ops（project_health）八个读取工具未钳沙盒；其中 project_health 叠加第二个 bug：未钳路径的子扫错误会被 `bug.get("total",0)` 吞成 0 问题、**返回假满分**。
- 交付：①八处补 `_fs_resolve` 钳制 + 统一错误信封 `try: … except ValueError as e: return {"error": str(e)}`，先于存在性检查（code_review 同款，S73 注记"读路径同样过沙盒"从纪律变代码）；project_health 钳制置于函数顶，越界拒绝时绝不给分。②tests/test_s88_sandbox_clamp.py 12 测：越界拒绝×7（含 bug_locate/search 默认 cwd 的"exe 缺失不得伪装成沙盒拒绝"甄别）、沙盒内正常路径可用性、project_health 不给分、junction 回归（skipif 非 Windows，mklink /J 建拆均有守卫）、auth_gate_sweep/path_probe 存续校验。③server.py 2.15.0；skills 四域契约声明（scan/search/game/ops）。
- 验证：S88 文件 12/12；3.14 全量 519 passed + 2 skipped（507 基线 + 12）；3.11 全量 521 passed；cargo test --release 95 绿、build 0 告警；selftest tools=57/GROUPS 12/SCHEMA_BAD 0。宿主 config.json 描述串仍写 v2.14.0（纯展示滞后，Yan Agent 运行中不动 config）。
- 提交：本次
