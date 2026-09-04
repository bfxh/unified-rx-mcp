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
