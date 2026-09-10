# zcode-breaker —— ZCode 工具熔断插件

同一（工具 + 参数）在窗口内重复超过阈值 → **阻断**；同一命令反复返回完全相同的结果 →
**阻断**；同一指令反复提交 → **告警**。与 `unified-rx-mcp` 的 `tools/breaker.py`
同规则（宿主级 + 工具级双保险）。

## 安装（推荐：本地市场）

1. 打开 **Settings → Plugin Management → Discover**，点右上角 **`+`**（Add marketplace）
2. 选择目录：`D:\开发\unified-rx-mcp\plugins\urx-marketplace`（内含 `marketplace.json`）
3. 在市场列表里找到 **zcode-breaker** → 点 **Get**
4. 到 **Installed** 页确认开关为开；点进详情页应能看到 **4 个 Hook** 且可运行

> 备用：把 `zcode-breaker/` 整个目录复制进 `~/.zcode/cli/plugins/cache/<marketplace>/zcode-breaker/1.0.0/`
> 并登记到 `installed_plugins.json`（不推荐——升级/卸载会绕开界面管理）。

## 规则与调参

| 事件 | 行为 |
|---|---|
| `PreToolUse` | 同一（工具+参数）在窗口内调用 **超过 limit 次** → 退出码 2 阻断（原因显示在客户端） |
| `PostToolUse` / `PostToolUseFailure` | 同一 key 连续返回**逐字节相同**结果达 limit 次 → 标记熔断，下一次同参调用被阻断 |
| `UserPromptSubmit` | 同一指令文本重复超 limit 次 → **只告警**（注入 additionalContext），不阻断输入 |

阈值在 `hooks/hooks.json` 每个 hook 的 `args` 里：`<mode> <limit> <window_s> <cooldown_s>`，
默认 **10 次 / 300 秒窗口 / 120 秒冷却**。改完即生效（钩子每次调用重新读参）。

- **旁路**：环境变量 `ZCODE_BREAKER=off`
- **复位**：删除 `%TEMP%\zcode-breaker\state.json`（按会话隔离，文件内分块）
- **依赖**：`python` 在 PATH 上（钩子用 `type: process` 直调，不走 shell）

## 诚实边界

- 钩子只看得到**工具调用与用户指令**，看不到模型自己的消息——所谓"消息重复"落在
  用户指令这一面；真正的循环刹车是工具调用面。
- 用户反复说"继续"是正常用法，所以 prompt 面**只告警不阻断**（要阻断就把该 hook 的
  `UserPromptSubmit` 段删掉或改脚本）。
- 参数指纹 = 排序后的 JSON 哈希：**语义等价但写法不同**的参数可能漏判（如键序无关，
  但 `"1"` 与 `1` 不同）。
- 这是**循环刹车，不是安全边界**：拦不住"每次换参数的穷举"。
- 状态落在 `%TEMP%`，系统清理临时目录时计数会归零（无害）。

## 与 MCP 侧熔断的关系

| 层 | 实现 | 覆盖 |
|---|---|---|
| 宿主级（本插件） | ZCode Hook，计数在 `%TEMP%\zcode-breaker\state.json` | **所有**工具（含 Bash/Read/Write/Agent 与任意 MCP 工具） |
| 工具级（MCP 内） | `unified-rx-mcp/tools/breaker.py` 挂在 `registry.call` | 该 MCP 的全部工具（进程内计数、`breaker_status`/`breaker_reset` 可查可复位） |

两层同规则、互不依赖：插件管全局，MCP 自己管自己（换宿主也带着刹车）。
