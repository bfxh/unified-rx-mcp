# ops 域（backup / scan_log / usage_stats / session_burn / lesson_stats）
- stats.jsonl 打点来自 registry.call 自动记录（usage_stats 数据源）
- backup 按 keep 滚动；scan_log 是调用日志查询
- session_burn（S141）：ZCode 会话 model-io 体积监测——体积≈累计 prompt 字节
  （MB×~28万≈token 量级），越 UNIFIED_RX_BURN_MB（默认 15）分级告警到
  alarms.jsonl；是"马拉松会话每轮重发全上下文"烧 token 的直接证据

## 渐进披露（S149）

**动机**：工具面每次会话都要占上下文（全量 80 件 ≈ 13.6K token 估算）；
"不需要每次把全部工具展给智能体看"——域级 profile 让宿主/agent 按需逐层放开。

| 工具 | 作用 |
|---|---|
| `profile_status`（读，恒在） | 当前启用的域、被裁掉的域、当前工具数/全量数 |
| `profile_enable`（**需授权**，恒在） | 开启一个或多个域；开启后发 `notifications/tools/list_changed`，宿主重拉 `tools/list` 即可见新工具 |

- **宿主裁剪**：启动 env `UNIFIED_RX_PROFILE=core`（= fs/scan/ide/search/ops/guard，
  54 件，≈9.5K token 估算）或逗号分隔域列表（如 `fs,sys`）；缺省 `all` 不变。
- **越域调用**：清晰拒绝并给指引（"先 profile_enable"），不是"未知工具"的模糊报错。
- **能力变更需批准**：`profile_enable` 挂 `__authorized` 门（对应 OWASP MCP
  "capability 变更需人工批准"）。
- **可测承诺**：`scripts/toolface_budget.py` 对 core 档设**软帽**（≤30,000 字符，
  实测 28,882）——裁剪面不许无声膨胀。

## 常驻服务（S151，命令行加速）

**问题**：每次工具调用起一个 exe ≈ 8-9ms 固定成本（Windows 进程创建）。
**做法**：`rx-svc.exe serve` 由 Python 宿主 `Popen` 起一次，之后请求/应答各一行
JSON 走它的 stdin/stdout——**没有监听端口、没有令牌**（只有父进程能写它的 stdin），
宿主退出即 EOF 自退（无孤儿）。覆盖域：sys / scan（bugscan·stdcheck·uicheck·
secrets）/ search / semantic / taint。**实测：std_check 5.53ms → 0.25ms（22.5×）**。

**质量不变**（硬约束）：服务不做缓存/状态，每个请求重跑与 CLI 相同的库函数；
`tests/test_s151_svc.py` 逐字节比对"服务回包 vs CLI stdout"（五域），并钉住
"服务不得比 CLI 慢 2×"（首版 TCP 环回 151ms/次的回归教训）。

**开关**：`UNIFIED_RX_SVC=off` 强制按次 spawn（测试默认 off，见 conftest）；
服务不可用/管道断/超时 → 自动回退 spawn，**行为与报错语义不变**。
首版走 TCP 环回被本机安全栈间歇拦截（min 0.4ms / 中位 15ms），已弃用改 stdio。
