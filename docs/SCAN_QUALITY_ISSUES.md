# unified-rx-mcp 扫描质量缺陷分析

> 2026-08-13 · 由 VoxelForge-Nexus 阶段 0（高危测试）触发核查
> 结论先行：**VoxelForge-Nexus 遇到的"大量问题"中，相当一部分是 unified-rx 扫描工具的
> 规则误报（噪音），而非真实缺陷**——但 H2 碎片归集机械组 bug 是真实缺陷（已修复），
> 二者必须区分。

---

## 一、背景

VoxelForge-Nexus `docs/GAME_DEV_TASKS.md`（RX project_scan 2026-08-13 产出）记录了：

```
bug_scan：246 条 warn（as 转换为主，多为体素坐标数学误报）；0 真 error
std_check：92 suggestion（占位文字/魔法数字，低危）
ui_check：3 warning
结论：代码级致命 bug 未扫出 → 拼接问题在运行时行为，必须实机验证暴露（V1-V10）
```

文档被迫给扫描结果写"多为体素坐标数学误报"的**辩解**——这是一个危险信号：
扫描工具的高噪音让 AI 无法区分"真问题"与"正常代码"，最终只能放弃扫描结论、
转投实机验证。本文档核查 unified-rx-mcp 源码，定位噪音来源并给出修复建议。

## 二、问题 A：`rust_scan.py` 的 `as` 规则大面积误报（主因）

### 2.1 证据链

`rust_scan.py:107-124`（type_cast_expression 分支）：

```python
dangerous = target in ("u8", "i8", "u16", "i16", "u32", "i32", "f32", "u64")
if dangerous:
    issues.append({... "severity": "warn", "rule": "as", ...})
```

- **危险目标白名单包含 `f32`/`u32`/`i32`/`u64`**——这些在 Rust 工程中是最常规的
  数值转换（坐标→浮点渲染、长度→f32 存储、u32 尺寸→i32 网格），并非截断风险。
- **无上下文判断**：不区分"窄化截断"（`i64 as i32`、`u64 as u32` 真危险）与
  "同宽/常规数学转换"（`u32 as i32` 坐标符号转换、`f64 as f32` 质量存储）。
- **无严重度分级**：一律 `warn`——噪音与真问题同等权重，淹没信号。
- 同类型转换也会报：如 `dims[0] as f32`（u32→f32）在 VoxelForge 每个模块定义
  的渲染路径都出现，直接贡献 232 条 warn。

`test_rustscan.py:65-79`（test_as_dangerous_only）**把误报固化为预期**：

```python
let b = x as f32;     // 危险：精度截断   ← 被断言为"危险"
let c = x as f64;     // 安全：不报
assert len(as_issues) == 2  # u8/f32
```

即"`as f32` = 危险"是**设计决定**，不是缺陷——但该决定缺少 Rust 生态共识：
clippy 的 `cast_precision_loss`（f64→f32 精度损失）是 **allow-by-default** 的
pedantic lint，生产代码普遍使用且被接受。

### 2.2 实测影响（VoxelForge-Nexus）

`scripts/rust_scan_all.py` 实测输出：46 文件 266 条问题中 **232 条是 `as`**，
其中绝大多数为坐标/尺寸/质量转换：

```
weld.rs:100  wg.total_mass += (sign * mass as f64) as f32;   ← 常规
weld.rs:121  (self.com_weighted[0] / self.total_mass as f64).round() as i32,  ← 常规圆整
placement.rs:210  hp: def.hp as f32,                          ← 常规
connectivity.rs:26  let idx = self.parent.len() as u32;       ← 常规（索引）
```

**探针复核（2026-08-13）**：构造 14 行含 4 种常规转换（`u32 as f32` 渲染尺寸、
`f64 as f32` 质量存储、`usize as u32` 索引、`f64→i32` 圆整）的样本扫 `scan_rust_file`——
**4 条常规转换全部被报为 warn，误报率 100%**；同规则下真实窄化（`i64 as i32`）反而
无法区分。误报为**规则设计**（`test_rustscan.py` 断言固化），非偶发。

**后果**：AI 面对 232 条 warn 只能整体忽略（"多为误报"），而真实的
`unwrap()`/`expect()`/`panic!`（约 30 条）也一并被淹没——噪音摧毁了扫描的
信噪比，扫描退化为"形式上扫了，实质上没用"。

## 三、问题 B：`bug_scan` 目录扫描 300s 超时（实测）

### 3.1 现象

2026-08-13 VoxelForge 会话实测：

```
mcp-tool:unified-rx/bug_scan (path=…\VoxelForge-Nexus\crates\nexus_core\src)
→ timed out after 300s
→ 同参数重试：成功（7 文件，8 issues，秒级）
```

第一次调用超时、重试成功——具备**冷启动/资源竞争**特征。

### 2.2 源码层候选原因（server.py）

1. **MCP 协议层**：`handle_call_tool` 用 `asyncio.to_thread(_call, ...)` 跑同步工具；
   工具执行期间若 `_spawn_self_scan()` 的 daemon 线程（自扫 600s / 项目 300s /
   全盘 1800s 循环）恰好进入全盘扫描，CPU 被抢占 → 首次调用饥饿。
2. **冷启动**：首次调用时 `import tree_sitter`/`tree_sitter_rust`（rust_scan.py:18-23）
   与 `mcp` 库懒加载在 to_thread 中首次执行，Windows 下解析器初始化慢。
3. **`_attach_known_issues`**（server.py:3199）每次工具返回前 `query_logs`
   全量读 `~/.unified-rx/scan-log.jsonl`（≤2000 行，数百 KB）——量级不大，
   但叠加高频调用时放大延迟。

> 判定：**超时现象属实，根因待复现定位**（候选：daemon 抢占 > 冷启动 > 日志回读）。
> 关键风险是**超时后 AI 会放弃扫描**——挖漏洞链条在"工具不可用"处断裂，
> 与"常态挖漏洞"规则的初衷相悖。

## 四、问题 C：噪音对下游决策的污染（间接危害）

1. **文档辩解化**：GAME_DEV_TASKS.md 为 246 条 warn 写"多为误报"——扫描结论
   失去权威性，AI 开始"凭印象"判断哪些是真问题（正是用户说的"幻觉"来源之一）。
2. **验证成本转移**：本该由扫描暴露的问题（如 H2 碎片归集机械组 bug）在
   246 条 warn 中完全不可见，最终靠 H2 高危测试（5 万次随机拆除）才抓到——
   **测试替扫描背了锅**。
3. **跨项目污染**：`scan-log.jsonl` 按 root 记录，但 AI 引用 `known_issues`
   时若不区分 root 会串项目（memory 已记载此风险）。

## 五、修复建议（按优先级）

> **修复状态（2026-08-13）**：P0/P1/P2 已全部落地并验证（199 测试全绿）。
> - P0：`rust_scan.py` `as` 三级分类（窄化 warn / 精度损失 info / 可能窄化 info / 加宽同宽跳过）
>   ——VoxelForge 实测 `as` warn **232 → 6 条**（212 条降 info），真 unwrap/panic 不再被淹没；
>   `test_rustscan.py` 断言同步更新（`test_as_severity_classified` / `test_as_f64_usize_i64_skipped`）。
> - P1：`_attach_known_issues` scan-log 回读加 **TTL 缓存**（mtime_ns+size key，5s）——
>   高频工具调用不再每次全量读文件。
> - P2：`bug_scan` 结果加 `severity_counts`（error/warn/info 归一化）+ `noise_ratio`（info 占比）
>   + `note` 说明——AI 一眼判断报告可信度。

### P0：`rust_scan.py` `as` 规则收窄（消除主噪音源）✅ 已修

- 危险目标收窄为**真实窄化**：`u8/i8/u16/i16`（截断）+ `i64/u64 → i32`
  （窄化风险）；**移除 `f32/u32/u64` 的默认 warn**。
- 加上下文启发：
  - 坐标/尺寸/质量字段（`dims`/`mass`/`hp`/`len`/`origin`/`com_weighted`）的
    `as f32`/`as i32`/`as u32` → 降级 `info` 或跳过；
  - 同宽转换（`u32 as i32`）默认跳过（符号转换非截断）。
- 严重度分级：`f64→f32` 用 `info`（对应 clippy `cast_precision_loss`，
  allow-by-default）；窄化用 `warn`。
- **同步更新 `test_rustscan.py:65-79` 的断言**（`x as f32` 从"危险"改"常规"），
  防止回归。

### P1：`bug_scan` 超时韧性

- daemon 扫描循环与工具调用共享 CPU 前做**互斥/降级**（如 daemon 全盘扫描时
  `bug_scan` 串行等待上限 10s，超时返回"扫描繁忙请重试"）。
- `_attach_known_issues` 的 `query_logs` 加**内存缓存**（mtime + 大小 key，
  5s TTL——对齐 toolcache 原则：白名单只读 + 成功才缓存）。
- 冷启动优化：`tree_sitter` 解析器**模块级单例**（已是）外，首次调用前
  预热（`_spawn_self_scan` 线程内预 import）。

### P2：信噪比度量与契约

- 扫描结果附 `noise_ratio`（如 as/info 占比）与 `critical`（error 数），
  让 AI 直接可判断"这份报告可信度"。
- `spec/` 工具契约补一条：**扫描工具必须区分"确定性缺陷"与"风格提示"**，
  默认按 severity 排序且 error 优先展示。

## 六、与 VoxelForge 真实问题的区分（重要）

本分析**不否定** VoxelForge-Nexus 存在真实问题：

| 项 | 判定 | 说明 |
|---|---|---|
| H2 碎片归集机械组 bug | ✅ **真实缺陷**（已修复） | 5 万次随机拆除风暴暴露：碎片混含结构+轮子时轮子被并入非机械组；`removal.rs` 已改为按旧 weld group 分区重建 |
| 232 条 `as` warn | ❌ **工具误报**（本文档问题 A） | 坐标/尺寸/质量常规转换被规则误标 |
| V1-V10 实机项 | ⏳ 待实机验证 | 静态扫描覆盖不到运行时拼接行为，需人工 `cargo run -p nexus_app` |

**结论**：修复扫描规则后，unified-rx 的扫描结论将恢复信噪比——
真实缺陷（unwrap/panic/除零/越界）不再被噪音淹没，AI 也能放心引用扫描结果，
而不是被迫"全凭实机"。

---

## 附：核查范围与证据

- 源码：`global-workspace/mcp-servers/unified-rx/rust_scan.py`、`server.py`、`scan_log_core.py`、`test_rustscan.py`
- 实测：VoxelForge-Nexus `scripts/rust_scan_all.py`（46 文件 / 266 条 / as 232 条）
- 实测：`bug_scan` MCP 首次调用 300s 超时、重试成功
- 相关文档：`docs/MCP_INTERFACE.md`（协议层）、`docs/REPORT_2026-08-13.md`（契约验证 13/13）

---

## 附二：2026-08-19 第二轮排查（工具导致 AI 智商降低专项）

> 用户要求："找找因为工具导致智商降低的各种情况然后修复 然后演进算法"。
> 本轮从"AI 消费工具输出"视角全面核查，修复 8 处 + 算法演进 3 条。

### 修复（噪音/漏报/误导，每项配测试或语义回归锚点）

| # | 问题 | 类型 | 修复 |
|---|---|---|---|
| R1 | `ciopt_` 52 工具 manifest 有、`_call` unknown tool | 能力清单幻觉 | 分发前缀补 `ciopt_`；语义回归全量可路由锚点 |
| R2 | `mcp_smoke.py` 断言旧工具名 `lesson_recall_lse`（已合并）→ pre-push 必挂 | 断言过时 | 断言更新 `lesson`、48→177 |
| R3 | `hallucination_guard` Windows 绝对路径 `C:\...\f.py:1` 提取丢盘符 → 真实声明判 unverifiable | 误判（AI 不敢引用事实） | `_PATH_PART` 支持盘符；lookbehind 排 `.` 防 URL 误报 |
| R4 | guard 行号超范围 → unverifiable（应 refuted） | 误判 | 已有 refuted 分支，路径修复后生效（测试断言更新） |
| R5 | `std_check` 魔法数字：`WINDOW_W = 1280` 常量定义误报；同行重复报 | 误报噪音 | 全大写常量定义豁免（小写变量仍报）+ 行内去重 |
| R6 | 文档工具数漂移 6 处（157/149/81/73 vs 实际 177/101） | 能力认知错误 | README/spec/AGENT_COMPAT/MEDIA_TOOLS/ARCHITECTURE/TOOL_INVENTORY 全部修正 |
| R7 | `items[len(items)]` 确定性越界漏报（AI 误信"0 issue"） | 漏报 | 新增 `_bug_check_len_index` 确定性规则（error 级） |
| R8 | 负索引字面量 `s[-3]` 越界漏报（AST UnaryOp 非 Constant） | 漏报 | `_bug_idx_value` 解析一元负号 |
| R9 | `z = 0` 后 `10 / z` 确定性除零漏报 | 漏报 | `zero_vars` 变量线性跟踪（重赋/`+=`/参数不报） |

### 演进原则（防再犯）

1. **确定性规则必须 error 级**：静态 100% 确定的缺陷不与其他 warning 混级
   （AI 按 severity 排序时 error 优先展示——spec §0.5 契约）。
2. **漏报与误报同罪**：漏报让 AI 误信"无问题"，误报让 AI 整体忽略扫描——
   两者都摧毁信噪比；每新增规则必须配"正例+反例"测试（安全模式不误报）。
3. **能力清单与实现必须一致**：manifest 列出的每个工具必须可路由
   （语义回归 6b 锚点：扩展全量实调，`unknown tool` 即红）。
4. **文档数字是契约**：工具数/测试数漂移会让 AI 引用错误事实——
   `tools.json` 一致性测试 + README 数字定期对账。
