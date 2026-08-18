# unified-rx-mcp 多智能体兼容（AGENT_COMPAT）

> 2026-08-13 · unified-rx server.py 是**标准 MCP stdio 协议**——任何支持 MCP 的
> 智能体都能连接。本文档说明：兼容矩阵、自动启动原理、接入方式、**优先适配 RX**。
> 2026-08-17 v2：新增 Qoder/WorkBuddy/Trae 用户级/Hermes（YAML）/Marvis/ZCode/QClaw
> 探测接入 + Reasonix Studio 适配（install_agents.py 四写入模式）。

---

## 一、兼容矩阵（打开智能体即自动启动）

| 智能体 | 项目级配置文件 | 启动即加载 | 接入方式 |
|---|---|---|---|
| **Reasonix（RX）/ Reasonix Studio** ✅ 优先 | `reasonix-plugin.json`（v2 manifest，`auto_start: true`）+ `.mcp.json` | ✅ 打开即自动启动 | 已装（路径已更新 D:\开发\unified-rx-mcp） |
| Claude Code | `.mcp.json` | ✅ | `scripts/install_agents.py --target claude` |
| Cursor | `.cursor/mcp.json` | ✅ | 同上 `--target cursor` |
| Windsurf | `.windsurf/mcp_config.json` | ✅ | 同上 `--target windsurf` |
| Trae | `.trae/mcp.json`（项目级） | ✅ | 同上 `--target trae` |
| Aider | `.aider.mcp.json` | ✅ | 同上 `--target aider` |
| Cline（VS Code） | `.cline/mcp_settings.json` | ✅ | 同上 `--target cline` |
| Roo Code | `.roo/mcp.json` | ✅ | 同上 `--target roo` |
| Qoder | `%APPDATA%\Qoder\User\settings.json`（用户级） | ✅ | 同上 `--target qoder`（已装 2026-08-17） |
| WorkBuddy | `%APPDATA%\WorkBuddy\User\settings.json`（用户级） | ✅ | 同上 `--target workbuddy`（已装 2026-08-17） |
| Trae CN（用户级） | `%APPDATA%\Trae CN\User\mcp.json` | ✅ | 同上 `--target trae-user`（已装 2026-08-17） |
| Trae SOLO | `%APPDATA%\TRAE SOLO CN\User\mcp.json` | ✅ | 同上 `--target trae-solo`（已装 2026-08-17） |
| Hermes Agent（华为） | `<Hermes>\data\hermes-home\config.yaml`（YAML `mcp_servers`） | ✅ | 同上 `--target hermes`（已装 2026-08-17，文本级更新保留注释/BOM） |
| Marvis | 探测 `~/.marvis` 等（应用内 UI 配置为主） | ⚠️ 需应用内配置 | `--target marvis`（无配置文件时输出手动指引） |
| ZCode / QClaw | 探测 `%APPDATA%` 等 | ⚠️ 需应用内配置 | `--target zcode` / `--target qclaw` |
| Gemini CLI / Codex CLI | `~/.gemini/settings.json` 等 | ✅ | 手写（见三） |

**原理**：所有条目都是标准 `mcpServers` 字典（`command` + `args` 指向
`server.py` 绝对路径）。智能体启动时自动 spawn MCP server 进程并握手——
无需每次手动启用。`server.py` 是纯 stdio 协议，无客户端特判。

## 二、接入方式

### 一键接入（推荐）

```bash
# 在目标项目目录运行（写入该项目根的项目级配置）
python <unified-rx 仓库>/scripts/install_agents.py --all
# 或只装某智能体
python <unified-rx 仓库>/scripts/install_agents.py --target qoder
# 预览不落盘
python <unified-rx 仓库>/scripts/install_agents.py --dry-run
# 指定项目
python <unified-rx 仓库>/scripts/install_agents.py --all --repo C:\path\to\project
```

脚本行为（安全设计）：
- 只合并 `mcpServers` 字典——**不删除**项目已有的其他 MCP 服务器条目
- `unified-rx` 条目用绝对路径（command=Python 解释器，args=[server.py 绝对路径]）
- 已有配置无法解析（坏 JSON）→ 跳过不覆盖（防破坏用户配置）

**四种写入模式**（v2，2026-08-17）：
- `project`：写入项目根相对路径（claude/cursor/windsurf/trae/aider/cline/roo）
- `user`：写入用户级配置（qoder/workbuddy/trae-user/trae-solo，`%APPDATA%` 展开，
  打开任意项目即加载）
- `yaml`：文本级更新 Hermes `config.yaml` 的 `mcp_servers` 块（保留注释与 BOM，
  幂等——重复执行只替换不叠加）
- `probe`：探测候选路径（marvis/zcode/qclaw），找到 JSON 配置就写，否则输出
  应用内手动配置指引（这些应用以 UI 配置 MCP 为主）

### 手写配置（任意智能体通用模板）

```json
{
  "mcpServers": {
    "unified-rx": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": ["C:\\<unified-rx 仓库路径>\\server.py"],
      "startup_timeout_seconds": 30,
      "call_timeout_seconds": 300
    }
  }
}
```

Gemini CLI（`~/.gemini/settings.json`）：

```json
{
  "mcpServers": {
    "unified-rx": {
      "command": "C:\\<Python 绝对路径>\\python.exe",
      "args": ["C:\\<unified-rx 仓库路径>\\server.py"]
    }
  }
}
```

> 注：command 用**绝对路径**（防 PATH 劫持——与 RX 安装段一致）；不确定 Python 位置时
> Windows 用 `where python`、macOS/Linux 用 `which python3` 查询。

Codex CLI（`~/.codex/config.toml`）：

```toml
[mcp_servers.unified-rx]
command = "python"
args = ["C:\\<unified-rx 仓库路径>\\server.py"]
```

## 三、自动启动原理与行为

1. **协议**：`server.py` 用 `mcp.server.stdio.stdio_server()`（标准 MCP Python SDK）——
   JSON-RPC over stdio，任何 MCP 客户端可握手（initialize → tools/list → tools/call）。
2. **无客户端特判**：工具分发走统一 `_call`；`reasonix-plugin.json` 的 v2 字段
   （`auto_start`/`tier`）是 RX 扩展运行时专用，**其他智能体忽略即可**（标准
   `.mcp.json` 不带这些字段）。
3. **路径无关**：server.py 用 `__file__` 定位自身（`_REPO`/`_HERE`）；
   `UNIFIED_RX_SANDBOX`（默认=客户端工作目录）与 `UNIFIED_RX_PROJECT` 由环境变量
   控制——不同智能体各跑各的独立进程，互不干扰。
4. **rx-core 降级**：`RX_CORE=0` 或 rx-core 不存在时，纯 Python 路径自动接管
   （工具结果一致）——不依赖 RX 特有二进制。
5. **后台自扫**：`_spawn_self_scan()` 每客户端实例独立启动（自扫/项目/全盘循环），
   结果落盘 `~/.unified-rx/scan-log.jsonl`——多智能体共用同一日志区（按 root 过滤）。

## 四、优先适配 RX（本项目的首要目标）

- RX 是**第一优先**：`reasonix-plugin.json`（apiVersion `reasonix.io/plugin/v2`，
  `auto_start: true`，`startup_timeout_seconds: 30`）——打开 RX 自动启动，已生效。
- 工具契约/防幻觉闭环（`hallucination_guard` 回灌 LSE）与 RX 深度耦合；
  其他智能体连入得到**同一份证据与事实**，但 RX 专属链路（教训回灌/协作配方）
  以 RX 为完整形态。
- 后续智能体接入冲突（如某客户端不支持某项能力）以 RX 行为为准回退。

## 五、验证接入

```bash
# 1. 确认 server 可独立启动并握手
python server.py   # 会等 stdio——用 MCP 客户端连；或：
python scripts/mcp_smoke.py   # 冒烟：list_tools 数量

# 2. 确认配置 JSON 合法
python -c "import json;json.load(open('.mcp.json'))"

# 3. 重启智能体 → 对话里应出现 unified-rx 工具（177 = 101 核心 + 76 扩展；
#    见 tools.json / scripts/mcp_smoke.py 输出）
```
