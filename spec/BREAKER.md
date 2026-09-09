# BREAKER —— 工具熔断（S122，用户定调「监测到消息重复或同工具同命令执行超过 10 次自动触发熔断」）

> 目标：给"死循环"装刹车——agent 反复执行同一条命令时，**本地**把它断掉，
> 不靠模型自觉、不靠 API 侧参数（见 §四：那条路在本栈上走不通）。

## 一、规则（两层同口径）

| 层 | 实现 | 覆盖范围 | 状态 |
|---|---|---|---|
| **工具级** | `tools/breaker.py` 挂在 `registry.call`（门禁之后、缓存之前） | 本 MCP 全部工具 | 进程内计数；`breaker_status`/`breaker_reset` 可查可复位 |
| **宿主级** | `plugins/urx-marketplace/zcode-breaker`（ZCode Hook） | ZCode 里**所有**工具（Bash/Read/Write/Agent/任意 MCP） | `%TEMP%\zcode-breaker\state.json`，按会话分块 |

触发条件（两层一致）：

1. **重复调用**：同一 key（工具 + 规范化参数 + cursor）在 `WINDOW_S` 窗口内调用
   **超过** `LIMIT` 次 → 熔断，冷却 `COOLDOWN_S` 内该 key 一律拒绝；
2. **空转**：同一 key 连续返回**逐字节相同**的结果达 `LIMIT` 次 → 提前熔断
   （同一命令反复空转的强信号）；
3. **消息重复**（仅宿主级）：同一指令文本重复超 `LIMIT` 次 → **告警不阻断**
   （用户反复说"继续"是正常用法，阻断会把人锁在外面）。

默认 `LIMIT=10 / WINDOW_S=300 / COOLDOWN_S=120`；工具级用 `UNIFIED_RX_BREAKER_*`
环境变量覆盖，宿主级改 `hooks/hooks.json` 的 args。

**恢复路径**（三选一）：冷却到期自动恢复；**改参数/换目标即刻恢复**（key 变了）；
`breaker_reset`（工具级）或删除状态文件（宿主级）。

## 二、为什么是"刹车"不是"安全边界"

- 拦的是**重复**；"每次换参数的穷举"拦不住（那不是循环，是搜索）。
- 参数规范化 = JSON 排序序列化 + 哈希：语义等价但写法不同的参数可能漏判
  （键序无关；但 `"1"` 与 `1` 不同）。
- 熔断器自身**绝不抛穿**（registry 侧 try/except）：刹车坏掉不能拖垮工具；
  `breaker_status`/`breaker_reset` 自身豁免——救火的人不能被火拦住。
- 内存有界：最多跟踪 4096 个 key，超了清最老的（不能自己变成泄漏）。

## 三、与既有门禁的关系

- 挂在 `registry.call` 的**门禁之后、缓存之前**：缓存命中的重复调用同样是循环，
  不能绕过刹车（S103 缓存不削弱熔断）。
- 不改工具契约：熔断返回 `{ok:false, error:"BreakerOpen: ..."}`，与其它失败同形，
  调用方（模型）能读懂并改参数。
- 测试：`tests/test_s122_breaker.py`（工具级 8 测：阈值/冷却/滑窗/空转/旁路/豁免/
  registry 真链路/快照）+ `tests/test_s122_plugin_hook.py`（宿主级 9 测：真子进程
  同形验证 + hooks.json 清单对账）。

## 四、API 侧惩罚参数：本栈走不通（实测调研）

宿主实际调用的是 **智谱 GLM（`open.bigmodel.cn`，Anthropic 兼容端点，
模型 GLM-5.3-Flash）**。查证结论：

- 官方「核心参数」页只有 `do_sample` / `temperature` / `top_p`，
  **没有** `frequency_penalty` / `presence_penalty` / `repetition_penalty`；
- 第三方资料：GLM-4 系列支持两个惩罚参数（-2.0~2.0），但 **GLM-5 上不生效**；
- 宿主配置（`~/.zcode/v2/config.json`）只暴露 provider/model/推理档位与上下文上限，
  没有采样参数入口。

→ 靠"惩罚参数防死循环"在当前模型上不可用；**防死循环放在编排层**（本模块 + 宿主插件）。
不建议为此调高 temperature（会伤代码生成的确定性）。

## 五、宿主插件安装（ZCode）

见 `plugins/urx-marketplace/zcode-breaker/README.md`：
Settings → Plugin Management → Discover → `+` 添加本地市场
`D:\开发\unified-rx-mcp\plugins\urx-marketplace` → 安装 **zcode-breaker** →
Installed 页确认 4 个 Hook 可运行。

旁路 `ZCODE_BREAKER=off`；复位删除 `%TEMP%\zcode-breaker\state.json`。
