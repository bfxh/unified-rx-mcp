# unified-rx（MCP 服务器插件）

把 unified-rx-mcp 挂进 ZCode 的插件：安装后 ZCode 会以 stdio 方式启动
稳定版入口 `D:\rj\MCP\server.py`，全部 66 个工具以 `mcp__unified_rx__*` 出现。

## 启动参数

| 项 | 值 | 说明 |
|---|---|---|
| command | Python 3.14 绝对路径 | 宿主 spawn 不吃 shell PATH，用绝对路径稳 |
| args | `D:\rj\MCP\server.py` | **稳定版入口**（S90 纪律：开发版在 `D:\开发\unified-rx-mcp`） |
| PYTHONUTF8 | 1 | Windows 下强制 UTF-8 |
| UNIFIED_RX_SANDBOX | `D:\开发;D:\rj\MCP;C:\Users\lbx13\.zcode\workspace` | 工具能碰的路径白名单（分号分隔）。要操作别的目录，把根加进来 |

## 诚实边界

- 稳定版当前 2.39.0；`breaker_status`/`breaker_reset` 两个熔断工具在 2.40.0
  （feat/s122 已提交，GitHub 恢复后合并同步稳定版即带上来）。
- 沙盒外的路径会被工具拒绝并给原因，不静默降级。
- Rust 加速 exe 按 `%TEMP%\rx-rs-target\release` 惯例路径自动发现；缺失时报
  清晰错误，不静默降级。
