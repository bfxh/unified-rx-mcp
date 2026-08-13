#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""install_agents.py — unified-rx-mcp 多智能体一键接入（2026-08-13）。

unified-rx server.py 是标准 MCP stdio 协议——任何支持 MCP 的智能体都能连。
本脚本为常见智能体生成/合并接入配置（**打开智能体即自动启动本工具**）：

| 智能体        | 配置文件（项目级优先）            | 自动启动机制                |
|---------------|----------------------------------|---------------------------|
| Claude Code   | .mcp.json（项目根）               | 启动即加载 mcpServers       |
| Cursor        | .cursor/mcp.json                 | 启动即加载（可全局注册）     |
| Windsurf      | .windsurf/mcp_config.json         | 启动即加载                  |
| Trae          | .trae/mcp.json                    | 启动即加载                  |
| Aider         | .aider.mcp.json（项目级）          | 启动即加载                  |
| Cline (VS Code) | .cline/mcp_settings.json        | 启动即加载                  |
| Roo Code      | .roo/mcp.json                     | 启动即加载                  |

用法：
  python scripts/install_agents.py                 # 检测已安装智能体并写入（交互确认）
  python scripts/install_agents.py --target claude  # 只装指定智能体
  python scripts/install_agents.py --all           # 全部智能体（不询问）
  python scripts/install_agents.py --dry-run       # 只看要写什么，不落盘
  python scripts/install_agents.py --list          # 列出支持的智能体与路径
  python scripts/install_agents.py --target claude --repo C:\\path\\to\\project
                                                   # 指定项目目录（默认当前目录）

安全：只合并 mcpServers 字典（不删除其他服务器条目）；命令路径用绝对路径。
"""
import argparse
import json
import os
import sys

# server.py 默认取本仓库内（与脚本同级的 server.py）；可用 UNIFIED_RX_SERVER 覆盖
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)


def _default_python() -> str:
    return sys.executable


def _server_entry() -> dict:
    """unified-rx 服务器条目（命令 + 绝对路径 + 超时）。"""
    server_py = os.environ.get("UNIFIED_RX_SERVER") or os.path.join(_REPO, "server.py")
    return {
        "command": _default_python(),
        "args": [server_py],
        "startup_timeout_seconds": 30,
        "call_timeout_seconds": 300,
    }


# 智能体 → (配置文件名, 是否项目级, mcpServers 顶层键)
_AGENTS: dict[str, tuple[str, str]] = {
    # name -> (相对项目根的配置文件, mcpServers 所在顶层键)
    "claude": (".mcp.json", "mcpServers"),
    "cursor": (".cursor/mcp.json", "mcpServers"),
    "windsurf": (".windsurf/mcp_config.json", "mcpServers"),
    "trae": (".trae/mcp.json", "mcpServers"),
    "aider": (".aider.mcp.json", "mcpServers"),
    "cline": (".cline/mcp_settings.json", "mcpServers"),
    "roo": (".roo/mcp.json", "mcpServers"),
}


def _merge_servers(existing: dict | None, entry: dict) -> dict:
    """合并 mcpServers（保留已有其他服务器条目，unified-rx 覆盖更新）。"""
    servers = dict(existing or {})
    servers["unified-rx"] = entry
    return servers


def _write_agent(repo: str, name: str, entry: dict, dry_run: bool) -> dict:
    """为单个智能体写入配置。返回报告。"""
    rel_file, top_key = _AGENTS[name]
    repo = os.path.normpath(repo)  # Windows 路径规范化（防反斜杠丢失）
    path = os.path.join(repo, rel_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return {"ok": False, "agent": name, "path": path,
                    "error": f"已有配置无法解析（跳过，不覆盖）: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "agent": name, "path": path,
                "error": "已有配置不是 JSON 对象（跳过，不覆盖）"}
    data[top_key] = _merge_servers(data.get(top_key), entry)

    if dry_run:
        return {"ok": True, "agent": name, "path": path, "dry_run": True,
                "content": data}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True, "agent": name, "path": path, "written": True}
    except OSError as e:
        return {"ok": False, "agent": name, "path": path, "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="unified-rx 多智能体一键接入")
    ap.add_argument("--target", help="只装指定智能体（claude/cursor/windsurf/trae/aider/cline/roo）")
    ap.add_argument("--all", action="store_true", help="全部智能体（不询问）")
    ap.add_argument("--dry-run", action="store_true", help="只预览不落盘")
    ap.add_argument("--list", action="store_true", help="列出支持的智能体与配置文件")
    ap.add_argument("--repo", default=os.getcwd(), help="目标项目目录（默认当前目录）")
    args = ap.parse_args()

    if args.list:
        print("支持的智能体 → 项目级配置文件（打开智能体即自动启动 unified-rx）：")
        for name, (rel, _) in _AGENTS.items():
            print(f"  {name:<10} {rel}")
        print(f"\nserver: {os.path.join(_REPO, 'server.py')}")
        print("优先适配 RX：Reasonix 用 reasonix-plugin.json（auto_start=true，无需本脚本）")
        return 0

    targets = ([args.target] if args.target else
               (list(_AGENTS.keys()) if args.all else list(_AGENTS.keys())))
    entry = _server_entry()
    reports = []
    for name in targets:
        if name not in _AGENTS:
            print(f"未知智能体: {name}（可选: {', '.join(_AGENTS)}）")
            continue
        reports.append(_write_agent(args.repo, name, entry, args.dry_run))

    for r in reports:
        if r.get("ok"):
            mode = "预览" if r.get("dry_run") else "写入"
            print(f"[{mode}] {r['agent']:<10} {r['path']}")
            if r.get("dry_run"):
                print(json.dumps(r.get("content"), ensure_ascii=False, indent=2)[:600])
        else:
            print(f"[失败] {r.get('agent')}: {r.get('error')}")
    ok = sum(1 for r in reports if r.get("ok"))
    print(f"\n完成 {ok}/{len(reports)}；重启对应智能体即可自动启动 unified-rx"
          "（Claude Code/Cursor/Windsurf/Trae/Aider/Cline/Roo 均支持标准 MCP stdio）")
    return 0 if ok == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
