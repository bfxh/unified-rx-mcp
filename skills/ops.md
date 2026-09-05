# ops 域（backup/scan_log/usage_stats/project_health/lesson_stats）
- stats.jsonl 打点来自 registry.call 自动记录（usage_stats 数据源）
- backup 按 keep 滚动；scan_log 是调用日志查询
- **契约变化（S88）**：project_health 的 path 先过沙盒钳制——越界返回
  `{"error": "路径越界（沙盒外）：…"}` 且**不给分**（旧版越界路径会把子扫错误
  吞成 0 问题、返回假满分，已修）（S73 纪律补全）
