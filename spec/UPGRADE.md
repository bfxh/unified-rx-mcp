# unified-rx-mcp v2 → v2.5 升级技术方案

> 基于 2026-08-27 实测数据（自扫描 + 延迟剖析 + 1310 次真实调用统计 + MCP 规范对照）。
> 原则不变：不抢智能体的活；每个升级都必须过 EVAL.md 评分卡（≥75 分）。
> 优先级以"对 H1/H2/H3 的增益 ÷ 风险"排序，不做无证据的堆料。
>
> **与 ROADMAP P4 的关系**：P4（重启验证双注入/codegraph init/嵌入模型）是"挂载与生态"线，
> 本文档是"工具箱本体"线。先做完本表 S1-S3（安全+协议），再做 P4 验证，两者不冲突；
> 嵌入模型项维持 P4 的"资源约束（~2GB）评估后再说"，本文档不重复立项。

---

## 0. 现状基线（实测）

| 维度 | 数据 |
|---|---|
| 代码规模 | 23 个 .py，3375 行，最大单文件 tools/scan.py 443 行 |
| 工具面 | 39 工具 / 12 组，selftest 全绿 |
| 测试 | pytest 54 例全绿（含安全模糊集） |
| 真实调用量 | stats.jsonl 1310 条 |
| 调用 Top5 | ide_edit_multi(176) local_run(169) fs_read(149) fs_write(142) bug_scan(122) |
| 延迟瓶颈 | local_run avg **11.2s**（预期内，子进程）；process 881ms；engine_query 643ms |
| 快路径 | fs_read 1.3ms / bug_scan 14 文件 75ms / code_search 热 88ms —— 达标 |
| 自扫 | 源码干净；命中全部来自 `_pytest_tmp` 夹具残留（不是真问题） |

---

## A. 安全与正确性（P0，先做）

### A1. 授权绕过的系统性收口
**现状**：`__authorized` 校验已修成严格 `is True`（fs_write/ide_edit_multi/local_run 三处），
但它是**约定式**的——新增写工具时漏检查没有任何机制拦截。
**升级**：
```
registry.call() 增加声明式强制：注册时声明 requires_auth=True，
call() 统一校验 args.get("__authorized") is True，
工具函数签名删掉 __authorized 参数（一层防线，不再依赖每处手写 if）。
```
- 文件：registry.py + tools/fs.py ide.py meta.py
- 回归：test_security_fuzz 增"声明了 requires_auth 但调用缺参/伪造参 → 一律拒绝"
- 分值预估：安全边界 15/15

### A2. `_pytest_tmp` 夹具目录混入仓库根
**现状**：conftest 把 tmp_base 放进项目根，自扫会被夹具污染（本次审计 10 条假命中全是它），也不该被 git 追踪。
**升级**：tmp 目录改到 `%TEMP%\unified-rx-pytest\`（沙盒 env 单独放行该前缀），.gitignore 加行。
- 收益：self-scan 信噪归零、仓库干净。

### A3. 沙盒 TOCTOU 与重复 resolve
**现状**：每次工具调用都独立 realpath + 前缀比较；多段路径场景下 `../` 已被 resolve 吸收，安全。
**升级（低优先）**：fs.py 内 `_in_sandbox` 结果按 (path, roots) 做进程内 LRU 缓存，避免重复磁盘 resolve；
同时保留 fail-closed 语义。分值：延迟预算+信噪各 +1，非必须。

### A4. server.py 尾部死代码
**现状**：L82-84 `if "__authorized" in args: pop 再放回` 是无效自我赋值（遗留混淆点）。
**升级**：删除，注释说明授权现在由 registry 层强制（配合 A1）。

---

## B. 协议面补齐（P1，影响 Hermes 注入体验）

对照 MCP 2025-03-26 实测缺失：`prompts/resources/completion/logging/progress/cancel/listChanged/_meta` 全 no。

### B1. `notifications/tools/list_changed`（做）
扫描器/引擎探测是动态能力（codegraph 有无会改变 engine_query 行为）。注册表变更时主动通知宿主刷新工具列表，避免宿主缓存过期 schema。
- 实现：server.py 加广播 hook；成本 <20 行。

### B2. `logging` 能力（做）
现在错误只有返回值一条路；long_run 的 local_run 后台任务进度无处可去。
- 实现：`notifications/message`（debug/info/warning），local_run background、engine_query 降级时发通知。
- 效果：Hermes 日志面板能看到工具层在干什么，调试成本大降。

### B3. `cancel` 支持（做）
local_run avg 11.2s 是最慢路径——用户关对话后智能体还可能等它。实现 `notifications/cancelled` 时在注册表分发处打取消旗标，长任务（local_run/engine_query/parallel）协作式中断。
- 实现：server.py 维护 request_id → threading.Event 映射；scan 循环/local_run 子进程轮询。
- 注意边界：这不抢判断权，只是让"已经决定取消的事"尽快落地。

### B4. `resources`（缓）
不建议现在做。资源订阅会把本工具箱变成半个 IDE；等 L3 replay-A/B 需要回放上下文时再评估。

---

## C. 性能与输出质量（P1，直接服务 H1 省 token）

### C1. 大结果集截断与分页协议化
**现状**：MCP 返回是 JSON 字符串串在一个 content 里；582 条命中如果 issues 不截断到 200 就是一次上下文轰炸（评分卡"输出信噪"明令禁止的事靠各工具自觉）。
**升级**：
```
registry.call 统一出口裁剪：默认 ≤200 项 / ≤50KB（对应 bug_scan 现状），
超出时 result 附 {"truncated": true, "total": N, "next_cursor": "..."}，
支持 args.cursor 分页续读。
```
- 收益：任何工具都不会再淹上下文；模型可按需翻页而不是一次性吃 50KB。
- 这是对 H1（省 input token）最直接的机械改进。

### C2. scan 类工具增量化
**现状**：bug_scan/project_scan 每次全量走 os.walk + 解析；14 文件 75ms 可接受，但大仓（400 文件上限）每次冷跑浪费。
**升级**：scan_log 已有 JSONL 记录，扩展为内容指纹表（mtime+size → 上次结果），
未变文件直接复用缓存命中。预计二次调用 <10ms。
- 门槛：必须保证 mtime 粒度足够（Windows NTFS 100ns）；测试用 touch 断言缓存失效。
- 注意：这是纯体力优化，不改变扫描语义。

### C3. usage_stats/cost_report 输出瘦身
现状 freq_top/slowest_top 结构合理但无时间窗口过滤参数拼接冗余；保持不动也能接受。
低优先。

---

## D. 工具语义升级（P2，只动"规则可枚举"域）

### D1. bug_scan 规则精度工程（对应 H3 整改）
L2 首测证明文本密度与修复无关。升级行动：
1. 规则分级重构：把 `unwrap/as_cast/indexing` 从 severity=high 降为 info + "线索"标记，
   high 只留给**跨函数可判定**模式（undefined_name、load-after-free 型、equal_float、裸except吞错、Bevy query_single 在 exclusive system）
2. 每条规则配**反例标注**：从 VoxelForge git log 固化的 30 条真 bug 库逐条验 precision，
   precision<0.5 的规则自动降级或删除——规则本身也要过质量卡尺
3. Rust 侧增加 AST 级规则（借 codegraph 符号图判 unwrap 是否在 test mod / 是否有 ? 兜底），
   把 geom.rs tests 那类误报在生产标记阶段消除
- 度量：precision≥0.7 才允许 severity≥medium（EVAL-L2 卡尺）。

### D2. locate_edit/context 增强"改前上下文"
Top1 工具是 ide_edit_multi——使用画像说明最高频的活是定点修改。
升级：locate_edit 返回候选时附带**该符号的被引用数**（BM25 命中计数已有），
让模型一眼看出改动影响面；ide_edit_multi 应用前自动 diff 预览行数统计（<3 行差异直接附上 diff 文本）。
- 不越界：仍不输出解法，只给事实（引用计数/diff 形状）。

### D3. engine_query 与 codegraph 更深握手
现状 BM25 降级工作正常。升级：当目标项目有 `.codegraph/` 时，
`code_context/locate_edit` 直接复用其符号表做精确 file:line 定位（代替文本近似），
探测逻辑走现有 engine_status。有则精确定位，无则现行为，零风险渐进。

### D4. pipeline 配方扩容（可选）
现有 audit_repo/guard_text/locate_context 三配方。基于调用画像补两个高频组合：
- `edit_guard`：ide_edit_multi 前 → 自动 hallucination_guard 核对该文件相关声明
- `pre_commit`：project_scan + std_check + backup 增量快照一键链
- 门槛：D4 每个配方都要在 selftest 抽样验证。

---

## E. 可评测性固化（P2，让升级可以被证伪）

### E1. 对外发布 `bench/`
- `bench/replay_ab.py`：L3 runner 骨架（双臂并发、rubric 判分、tokens/walltime 记账）
- `bench/labeled_bugs.jsonl`：30 条 VoxelForge 历史 bug 标注库（本次 audit 已手工确认样例格式）
- `tools/run_l2.ps1`：一行重算 P/R，作为 CI 门禁（分数倒退 = 合并拒绝）

### E2. capability_manifest 版本化
manifest 增加 `revision` 字段与生成时间戳；ROADMAP/SPEC/EVAL 三文档交叉引用同一 revision，
防文档漂移（hallucination_guard 自己先吃到自己碗里）。

---

## F. 不做什么（同样重要）

| 提案 | 否决理由 |
|---|---|
| Python→Rust 重写 | 无性能压力（快路径已达 ms 级）；Rewrite 是最大风险源 |
| resources/prompts 全面跟进 MCP 规范 | 当前宿主用不到；维护面积 ↑ 收益 ↓ |
| 自动修码工具（auto_fix） | 直接违反"不抢智能体活"；建议永远由模型出 patch、工具只验 diff |
| 云端同步教训库 ~/.unified-rox | 隐私边界 + 复杂度；本地 jsonl 够用 |
| 更多语言新规则一把梭 | 先过 D1 的 precision 门槛再说，规则数量不是 KPI |

---

## G. 施工顺序与验收

| 步骤 | 内容 | 验收命令 | 状态 |
|---|---|---|---|
| S1 | A1+A2+A4 授权收口/夹具隔离/清死代码 | pytest 新增 4 例绿（requires_auth 声明校验/registry 层拦伪造/local_run 拦截/夹具不在仓库根） | ✅ PR#15 (c634e53) |
| S2 | C1 出口裁剪+游标分页 | pytest 3 例；MCP stdio 端到端：593 命中 → 200+393 分页，伪造授权经协议层被拒 | ✅ PR#15 (c634e53) |
| S3 | B1+B2+B3 协议通知三件套 | capabilities 声明 listChanged+logging；降级发 notifications/message；cancelled→cancel_flag 登记（完成即清）；pytest 4 例 + stdio 实测 | ✅ PR#15 (7bcfac5) |
| S4 | D1 规则分级重构 | clue/definite 双字段，确定性崩溃才 high；#[cfg(test)] 行级降级。VoxelForge: high 88→21（唯一 high=panic!） | ✅ PR#15 (3772dfb) |
| S5 | C2 扫描指纹缓存 | bug_scan 77ms→2.7ms、std_check 16ms→1.8ms；mtime_ns+size 失效验证 | ✅ PR#16 (34fc149) |
| S6 | E1 bench 骨架 + D2 引用计数 + 标注库 | replay_ab.py dry-run 入 CI 门禁；labeled_bugs.jsonl（9/10 诚实标注"文本规则不可判"） | ✅ PR#16 (210ea54) |
| S7 | 攻击域默认化 input_fuzz/path_probe/big_input | locate_edit 空查询噪音 FAIL-noise 暴露并修复；错误语义统一 `{ok:false}` | ✅ PR#16 (f4a8951) |
| S8 | appaudit 域：智能体/桌面应用 克隆→隔离审计→清理 三件套 | 见下方 H 节实测；asar 自标定提取器在真实 Electron 包上跑通 | ✅ 本轮 |

**S4 施工追加**：bevy 迁移类规则同步降为 info/clue；通用规则补 severity/kind
字段（equal_float 此前缺 severity，by_severity 统计漏计）。

**C1 分页契约（定稿）**：列表字段超 `MAX_RESULT_ITEMS(200)` 时截断；响应附
`total_items` / `truncated`（仅当还有下一页）/ `next_cursor`（仅当还有下一页）；
请求传 `cursor` 续读（传输层参数，工具签名不含它）；坏游标按第 0 页、越界空页。
末页不带 truncated/next_cursor —— 消费方循环条件：`next_cursor` 存在。

**S8 施工追加（克隆隔离三保证）**：
1. 副本唯一落点 `%TEMP%\unified-rx-appaudit\<ts>-<sha256前12>-<净化名>`，目录名哈希派生不吃原始路径注入
2. `app_audit` 结构性拒绝沙箱外路径——审原件必须先 `app_clone`，没有旁路
3. `app_clean` 同样限内 + requires_auth；秘密只回掩码（前6字符+长度），原值不落审计输出

**S8 实测自证（对本机真实安装的 Yan Agent，v2.2.0）**：

| 面 | 结果 |
|---|---|
| 程序目录克隆 | 3311 files / 1.21GB / verified=True |
| 用户数据克隆 | plan=disk=4303（4332−29 锁定缓存文件显式 read_fails）/ verified=True |
| asar 提取 | 真实 27MB app.asar 自标定成功，329 条目重扫；main.js openExternal=4 与上轮人工审计互证 |
| 发现 | definite=0；clue 92 条全部有文件行号与掩码。要点：config.json 内成组 sk-key（6 行，两种长度）；eval 全部来自 effect npm 库内部实现 |

施工中当场被抓的两个真缺陷（都被测试锁死）：
- `.pem/.key/.crt` 不在可扫扩展集 → 私钥规则对真实凭据文件全盲（合成 mini-app 测试暴露）
- `shutil.copyfile` 先建目标再开源 → 锁定源留下 **0 字节残桩**污染克隆（verified 计数差暴露）；改先开源句柄，失败即清理放弃

每个 S 完成即跑全套：`--selftest` + `pytest tests/ -q` + `_mcp_probe.py`，三者任一红即停线回查。
当前基线：**82 passed / selftest 45 工具 SCHEMA_BAD 0 / server v2.2.0**。
复用入口：`python bench/agent_selfcheck.py <安装目录> [--data-dir <用户数据>] [--clean]`
返回码 0 干净 / 1 调用失败 / 2 有 definite 待人工确认 —— 以后"遇到智能体自己查自己"的默认动作。
