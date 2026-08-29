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
