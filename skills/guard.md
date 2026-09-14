# guard 域（capability_manifest / hallucination_guard / breaker_status / breaker_reset）
- hallucination_guard：文件/符号声明 vs 真值校验（路径存在性、行号界内）
- S24 H2 口径：1379 条声明一致率 100%——但那是文件判定强项，语义维度只出 unverifiable
- **breaker_status（S122，S131 自 meta 域归位）**：工具熔断状态——窗口内重复调用计数、
  已熔断的 key、阈值与旁路开关。同一（工具+参数）在窗口内调用超过 limit 次即熔断
- **breaker_reset（S122，S131 自 meta 域归位）**：复位熔断（给 `tool` 只复位该工具；
  缺省全清）

## 工具熔断（S122）速查

| 项 | 值 |
|---|---|
| 触发 | 同一（工具 + 规范化参数 + cursor）在 `WINDOW_S` 内调用 **> LIMIT** 次 |
| 空转 | 同一 key 连续返回**逐字节相同**的结果达 LIMIT 次 → 提前熔断 |
| 默认 | LIMIT=10 / WINDOW_S=300 / COOLDOWN_S=120（env `UNIFIED_RX_BREAKER_*` 覆盖） |
| 旁路 | `UNIFIED_RX_BREAKER=off` |
| 恢复 | 冷却到期自动恢复；**改参数/换目标即刻恢复**（key 变了）；`breaker_reset` 复位 |
| 豁免 | `breaker_status` / `breaker_reset` 自身永不熔断（救火的人不能被火拦住） |

定位：**循环刹车，不是安全边界**——拦重复，不拦"每次换参数的穷举"。宿主级同规则
插件见 `plugins/urx-marketplace/zcode-breaker`（ZCode Hook，覆盖所有工具）；
设计取舍见 `spec/BREAKER.md`。
