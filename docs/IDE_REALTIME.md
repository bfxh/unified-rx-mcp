# IDE 实时双向反馈 · 预知 · 推测执行

> 2026-08-15 · 三件套落地（阶段1-4）+ 全量 306 绿 · 66 核心工具

---

## 一、实时触发（阶段1 · realtime_watch.py）

**文件改动 → 即时反馈**（2s 指纹轮询——复用 shadow_core mtime 机制，不引入 watchdog 依赖）：

```
文件改动 → 指纹变化检测（mtime_ns:size）→ 增量扫描（bug_scan+std_check 单文件）
        → scan-log 打点（tool=watch_bug/watch_std）→ known_issues 更新
```

- `watch_once()`：单轮检测+扫描（测试/手动）；`WatchLoop`：常驻 daemon 线程（server 启动即拉）
- 可配：`UNIFIED_RX_WATCH_INTERVAL`（默认 2s）/ `UNIFIED_RX_WATCH_ROOTS`（分号分隔根）
- `watch_status` 工具：监听线程状态（运行/间隔/根/跟踪文件数）
- 安全：深度 ≤3 + 文件数 ≤500（防大仓库爆炸）；白名单扩展名复用

## 二、预知引擎（阶段2 · predict_impact.py）

**"你要改 X，预测会破坏 Y"**——改前预测（与 `ide_fusion impact` 改后确认互补）：

```
predict_impact(root, symbol, file_hint)
  ├─ ① 影响面预测：ide_references 引用图 → 调用方文件/行数
  ├─ ② 风险教训：lse 教训库匹配（同类符号/文件名历史坑）
  └─ ③ 规则提示：目标文件静态红线（unwrap/无限循环/帧内 IO）
  → risk 分级（high/medium/low）+ 建议"改后跑 ide_fusion 确认"
```

- 全部只读（预测不执行任何写操作）；未知符号诚实拒绝

## 三、推测执行（阶段3 · speculate.py）

**预测下一步工具调用 → 预执行 → 缓存秒回**（浏览器 preload 思路）：

```
编辑上下文（当前文件/最近调用）→ 启发式预测 1-3 个下一步调用
  → 预执行（仅幂等只读白名单：bug_scan/std_check/cb_scan/locate_edit/
    game_check/ide_references/cb_status）
  → 结果进推测缓存（TTL 60s / 上限 200）
  → 实际调用命中缓存 → 秒回（_call 消费——不重复执行）
```

- **安全边界**：白名单严格只读（写工具绝不预执行）；单次 ≤3 个；缓存键=工具+参数签名（防错缓存）
- `speculate` 工具：手动触发预测+预执行；stats（predicted/executed/hit/errors）可见

## 四、双向反馈回路（阶段4 · runtime_state）

**运行状态回喂 + 结果实时提示**（闭合：代码→运行→反馈→代码）：

```
运行时状态（Bevy BRP localhost:15702 实体 / 文件指纹 / 直接上报）
  → scan-log（tool=runtime_state）→ 对话随时可查最新运行反馈
```

- BRP 未运行 → **诚实降级**（记录降级 + 提示启动游戏含 bevy_remote 插件——不崩溃）
- 与 scan-log known_issues（结果→调用方反馈）形成**双向闭环**

---

## 五、验证

| 项 | 结果 |
|---|---|
| 全量测试 | **306 passed**（296 + 10 新） |
| 工具数 | 62 → **66**（watch_status/predict_impact/speculate/runtime_state） |
| 关键测试 | 改动→检测→打点闭环 / 预测影响面+规则提示 / 预测→预执行→缓存命中秒回 / BRP 降级 |

## 六、边界与后续

- 推测执行启发式（编辑后扫描/查标准/定位）——后续可接 stats 调用模式学习（数据驱动预测）
- BRP 为可选通道（bevy_remote 插件 + localhost:15702）——游戏运行时可查实体状态
- 监听轮询 2s（够实时且开销小）——超大仓库可调大间隔
