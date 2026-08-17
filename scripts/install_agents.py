#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""install_agents.py — unified-rx-mcp 多智能体一键接入 v2（2026-08-17）。

unified-rx server.py 是标准 MCP stdio 协议——任何支持 MCP 的智能体都能连。
本脚本为常见智能体生成/合并接入配置（**打开智能体即自动启动本工具**）：

| 智能体        | 配置文件                          | 模式     |
|---------------|-----------------------------------|----------|
| Claude Code   | .mcp.json（项目根）                | project  |
| Cursor        | .cursor/mcp.json                  | project  |
| Windsurf      | .windsurf/mcp_config.json          | project  |
| Trae          | .trae/mcp.json（项目级）           | project  |
| Aider         | .aider.mcp.json（项目级）          | project  |
| Cline (VS Code) | .cline/mcp_settings.json        | project  |
| Roo Code      | .roo/mcp.json                     | project  |
| Qoder         | %APPDATA%\\Qoder\\User\\settings.json | user   |
| WorkBuddy     | %APPDATA%\\WorkBuddy\\User\\settings.json | user |
| Trae CN(用户级) | %APPDATA%\\Trae CN\\User\\mcp.json | user |
| Trae SOLO     | %APPDATA%\\TRAE SOLO CN\\User\\mcp.json | user |
| Hermes Agent  | <Hermes>\\data\\hermes-home\\config.yaml | yaml |
| Marvis        | 探测 ~/.marvis 等（UI 内配置为主）   | probe   |
| ZCode         | 探测 %APPDATA%\\ZCode 等            | probe   |
| QClaw         | 探测 %APPDATA%\\QClaw 等            | probe   |

用法：
  python scripts/install_agents.py                 # 检测已安装智能体并写入（交互确认）
  python scripts/install_agents.py --target qoder   # 只装指定智能体
  python scripts/install_agents.py --all           # 全部智能体（不询问）
  python scripts/install_agents.py --dry-run       # 只看要写什么，不落盘
  python scripts/install_agents.py --list          # 列出支持的智能体与路径
  python scripts/install_agents.py --target claude --repo C:\\path\\to\\project
                                                   # 指定项目目录（默认当前目录）

安全：只合并 mcpServers 字典（不删除其他服务器条目）；命令路径用绝对路径；
     已有配置无法解析 → 跳过不覆盖（防破坏用户配置）。
"""
import argparse
import json
import os
import re
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


def _expand(p: str) -> str:
    """展开 %APPDATA%/%USERPROFILE%/%LOCALAPPDATA% 占位符（Windows）。"""
    for var in ("APPDATA", "USERPROFILE", "LOCALAPPDATA"):
        if var in os.environ:
            p = p.replace(f"%{var}%", os.environ[var])
    return p


def _hermes_config() -> str:
    """Hermes Agent 的 config.yaml（固定安装在 D:\\rj\\AI 下，或 HERMES_HOME 覆盖）。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return os.path.join(env, "config.yaml")
    return r"D:\rj\AI\Hermes Agent CN Desktop\data\hermes-home\config.yaml"


# 智能体 → (模式, 配置路径, mcpServers 顶层键)
#   project: 相对项目根写入 JSON
#   user:    用户级绝对路径写入 JSON（%APPDATA% 展开）
#   yaml:    文本级更新 YAML 块（保留注释）
#   probe:   从候选路径探测，找到 JSON 配置就写，否则报告
_AGENTS: dict[str, tuple[str, str, str]] = {
    # name -> (模式, 配置路径, mcpServers 所在顶层键)
    "claude": ("project", ".mcp.json", "mcpServers"),
    "cursor": ("project", ".cursor/mcp.json", "mcpServers"),
    "windsurf": ("project", ".windsurf/mcp_config.json", "mcpServers"),
    "trae": ("project", ".trae/mcp.json", "mcpServers"),
    "aider": ("project", ".aider.mcp.json", "mcpServers"),
    "cline": ("project", ".cline/mcp_settings.json", "mcpServers"),
    "roo": ("project", ".roo/mcp.json", "mcpServers"),
    # 用户级（VSCode 系，打开即自动加载）
    "qoder": ("user", r"%APPDATA%\Qoder\User\settings.json", "mcpServers"),
    "workbuddy": ("user", r"%APPDATA%\WorkBuddy\User\settings.json", "mcpServers"),
    "trae-user": ("user", r"%APPDATA%\Trae CN\User\mcp.json", "mcpServers"),
    "trae-solo": ("user", r"%APPDATA%\TRAE SOLO CN\User\mcp.json", "mcpServers"),
    # YAML（Claude Code 系运行时）
    "hermes": ("yaml", _hermes_config(), "mcp_servers"),
}

# 探测式智能体：候选配置文件路径列表（存在则按 JSON 合并，否则报告手动配置）
_PROBE_AGENTS: dict[str, list[str]] = {
    "marvis": [
        r"%USERPROFILE%\.marvis\mcp.json",
        r"%APPDATA%\Marvis\mcp.json",
        r"D:\rj\AI\MarvisData\mcp.json",
    ],
    "zcode": [
        r"%APPDATA%\ZCode\mcp.json",
        r"%USERPROFILE%\.zcode\mcp.json",
    ],
    "qclaw": [
        r"%APPDATA%\QClaw\mcp.json",
        r"%USERPROFILE%\.qclaw\mcp.json",
    ],
}

_ALL_AGENTS = sorted(set(_AGENTS) | set(_PROBE_AGENTS))


def _merge_servers(existing: dict | None, entry: dict) -> dict:
    """合并 mcpServers（保留已有其他服务器条目，unified-rx 覆盖更新）。"""
    servers = dict(existing or {})
    servers["unified-rx"] = entry
    return servers


def _write_json(path: str, top_key: str, entry: dict, dry_run: bool) -> dict:
    """合并写 JSON 配置（mcpServers/mcp_servers 顶层键，兼容 UTF-8 BOM）。"""
    data: dict = {}
    bom = False
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            bom = text.startswith("\ufeff") or open(path, "rb").read(3) == b"\xef\xbb\xbf"
            data = json.loads(text.lstrip("\ufeff"))
        except (json.JSONDecodeError, OSError) as e:
            return {"ok": False, "path": path, "error": f"已有配置无法解析（跳过）: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "path": path, "error": "已有配置不是 JSON 对象（跳过）"}
    data[top_key] = _merge_servers(data.get(top_key), entry)
    if dry_run:
        return {"ok": True, "path": path, "dry_run": True, "content": data}
    try:
        body = json.dumps(data, ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
            f.write(body)
        return {"ok": True, "path": path, "written": True}
    except OSError as e:
        return {"ok": False, "path": path, "error": str(e)}


def _yaml_block_lines(entry: dict) -> list[str]:
    """unified-rx 的 YAML 块（2 空格缩进，匹配 Hermes config.yaml 风格）。"""
    return [
        "  unified-rx:",
        f"    command: {entry['command']}",
        "    args:",
        *[f"      - {a}" for a in entry["args"]],
        f"    timeout: {entry.get('call_timeout_seconds', 300)}",
        f"    connect_timeout: {entry.get('startup_timeout_seconds', 60)}",
    ]


def _upsert_yaml_block(path: str, entry: dict, dry_run: bool) -> dict:
    """文本级更新 YAML 的 mcp_servers.unified-rx 块（保留注释与无关内容）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        return {"ok": False, "path": path, "error": str(e)}

    block = _yaml_block_lines(entry)
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^mcp_servers:\s*(#.*)?$", ln):
            start = i
            break
    if start is None:
        # 文件末尾追加新块
        lines.append("\nmcp_servers:\n")
        start = len(lines) - 1

    # 找 mcp_servers 块内的 unified-rx 子块（缩进 2 空格）
    sub_start = None
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if re.match(r"^  unified-rx:\s*(#.*)?$", ln):
            sub_start = i
            break
        if re.match(r"^\S", ln) and not ln.startswith("#"):
            break  # 顶格新键 = mcp_servers 块结束
    if sub_start is not None:
        # 子块结束 = 下一个非空且缩进 < 2 的行
        sub_end = sub_start + 1
        while sub_end < len(lines):
            ln = lines[sub_end]
            if ln.strip() and not ln.startswith("  ") and not ln.startswith("#"):
                break
            sub_end += 1
        new_lines = lines[:sub_start] + [b + "\n" for b in block] + lines[sub_end:]
    else:
        # 插入到 mcp_servers: 行之后（保持块内顺序）
        new_lines = lines[: start + 1] + [b + "\n" for b in block] + lines[start + 1:]

    if dry_run:
        return {"ok": True, "path": path, "dry_run": True, "content": "".join(new_lines)}
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return {"ok": True, "path": path, "written": True}
    except OSError as e:
        return {"ok": False, "path": path, "error": str(e)}


def _write_agent(repo: str, name: str, entry: dict, dry_run: bool) -> dict:
    """为单个智能体写入配置。返回报告。"""
    mode, rel_file, top_key = _AGENTS[name]
    if mode == "project":
        repo = os.path.normpath(repo)
        path = os.path.join(repo, rel_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return {"agent": name, **(_write_json(path, top_key, entry, dry_run))}
    if mode == "user":
        path = _expand(rel_file)
        if not os.path.exists(os.path.dirname(path)):
            return {"ok": False, "agent": name, "path": path,
                    "error": "未找到该智能体的用户配置目录（可能未安装？）"}
        return {"agent": name, **(_write_json(path, top_key, entry, dry_run))}
    if mode == "yaml":
        path = _expand(rel_file)
        if not os.path.exists(path):
            return {"ok": False, "agent": name, "path": path,
                    "error": "未找到 Hermes config.yaml（可用 HERMES_HOME 指定）"}
        return {"agent": name, **(_upsert_yaml_block(path, entry, dry_run))}
    return {"ok": False, "agent": name, "error": f"未知模式: {mode}"}


def _write_probe(name: str, entry: dict, dry_run: bool) -> dict:
    """探测式写入：候选路径存在则合并，否则报告手动配置指引。"""
    for rel in _PROBE_AGENTS[name]:
        path = _expand(rel)
        if os.path.exists(path):
            return {"agent": name, "mode": "probe",
                    **(_write_json(path, "mcpServers", entry, dry_run))}
    # 未找到配置：给出该智能体的手动配置指引
    return {"ok": False, "agent": name, "mode": "probe",
            "error": f"未发现 {name} 的 MCP 配置文件（候选: {', '.join(_PROBE_AGENTS[name])}）。"
                     f"请在 {name} 应用内配置 MCP，添加 unified-rx: {entry}"}


def _out(s: str) -> None:
    """打印（UTF-8 stdout，Windows GBK 终端兼容；去 BOM）。"""
    try:
        print(s.replace("\ufeff", ""))
    except UnicodeEncodeError:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(s.replace("\ufeff", ""))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="unified-rx 多智能体一键接入 v2")
    ap.add_argument("--target", help=f"只装指定智能体（{', '.join(_ALL_AGENTS)}）")
    ap.add_argument("--all", action="store_true", help="全部智能体（不询问）")
    ap.add_argument("--dry-run", action="store_true", help="只预览不落盘")
    ap.add_argument("--list", action="store_true", help="列出支持的智能体与配置文件")
    ap.add_argument("--repo", default=os.getcwd(), help="目标项目目录（默认当前目录）")
    args = ap.parse_args()

    if args.list:
        _out("支持的智能体 → 配置文件（打开智能体即自动启动 unified-rx）：")
        for name in sorted(_AGENTS):
            mode, rel, _ = _AGENTS[name]
            _out(f"  {name:<12} [{mode:<7}] {rel}")
        for name in sorted(_PROBE_AGENTS):
            _out(f"  {name:<12} [probe  ] {', '.join(_PROBE_AGENTS[name])}")
        _out(f"\nserver: {os.path.join(_REPO, 'server.py')}")
        _out("优先适配 RX：Reasonix 用 reasonix-plugin.json（auto_start=true，无需本脚本）")
        return 0

    targets = ([args.target] if args.target else
               (list(_AGENTS) + list(_PROBE_AGENTS) if args.all else
                list(_AGENTS) + list(_PROBE_AGENTS)))
    entry = _server_entry()
    reports = []
    for name in targets:
        if name in _AGENTS:
            reports.append(_write_agent(args.repo, name, entry, args.dry_run))
        elif name in _PROBE_AGENTS:
            reports.append(_write_probe(name, entry, args.dry_run))
        else:
            print(f"未知智能体: {name}（可选: {', '.join(_ALL_AGENTS)}）")
            continue

    for r in reports:
        if r.get("ok"):
            mode = "预览" if r.get("dry_run") else "写入"
            _out(f"[{mode}] {r['agent']:<12} {r.get('path', '')}")
            if r.get("dry_run"):
                content = r.get("content")
                if isinstance(content, dict):
                    _out(json.dumps(content, ensure_ascii=False, indent=2)[:600])
                else:
                    _out(str(content)[:600])
        else:
            _out(f"[失败] {r.get('agent')}: {r.get('error')}")
    ok = sum(1 for r in reports if r.get("ok"))
    _out(f"\n完成 {ok}/{len(reports)}；重启对应智能体即可自动启动 unified-rx"
         f"（{', '.join(sorted(_AGENTS))} 均支持标准 MCP stdio）")
    return 0 if ok == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
