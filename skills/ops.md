# ops 域（backup / scan_log / usage_stats / session_burn / lesson_stats）
- stats.jsonl 打点来自 registry.call 自动记录（usage_stats 数据源）
- backup 按 keep 滚动；scan_log 是调用日志查询
- session_burn（S141）：ZCode 会话 model-io 体积监测——体积≈累计 prompt 字节
  （MB×~28万≈token 量级），越 UNIFIED_RX_BURN_MB（默认 15）分级告警到
  alarms.jsonl；是"马拉松会话每轮重发全上下文"烧 token 的直接证据
