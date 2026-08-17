#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""local_tools.py — 本地工具注册表与安全调用桥（2026-08-17）。

用户要求（2026-08-17）："可以调用大部分的本地工具，在 RJ 里面就一堆"——
D:\\rj\\GJ（7zip/Blender/Everything/aria2/Umi-OCR 等 75+ 工具）。

- scan()    扫描本地工具根（默认 D:\\rj\\GJ + LOCAL_TOOL_ROOTS 扩展）
             → ~/.unified-rx/local-tools.json 注册表
- discover() 列出已注册工具（按类别）
- run()     安全调用桥：只允许注册过的工具 + 危险参数黑名单 + 超时 + 输出截断

安全设计：
- 只调已注册工具（注册=用户明示放入工具根），绝不任意执行路径
- 危险子串黑名单（rm -rf/format/del /s 等）在参数里出现即拒绝
- 超时默认 60s，输出截断 20000 字符（防撑爆上下文）
- 非交互命令为主（GUI 工具启动即返回；CLI 工具捕获输出）
"""
import json
import os
import re
import subprocess
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".unified-rx")
REGISTRY = os.path.join(STATE_DIR, "local-tools.json")

# 默认工具根（可 LOCAL_TOOL_ROOTS 覆盖，分号分隔）
_DEFAULT_ROOTS = [r"D:\rj\GJ", r"D:\rj\SJ", r"D:\rj\KF"]

# 排除：卸载器/更新器/崩溃处理器/下载器安装包
_EXCLUDE_NAME = re.compile(
    r"(unins\d*|uninstall|crashpad|update|updater|SodaDownloader|LiveUpdate|"
    r"\.7z$|\.zip$|\.html$|\.log$|\.json$)", re.IGNORECASE)
_EXCLUDE_DIR = re.compile(
    r"(360Safe|Archive|download_cache|temp|backup|\.git|node_modules|System|Tools)", re.IGNORECASE)
_EXTS = (".exe", ".cmd", ".bat", ".ps1")

# 危险参数子串（出现即拒绝）
_DANGEROUS = re.compile(
    r"(rm\s+-rf|rmdir\s+/s|format\s+[a-z]:|del\s+/[fsq]|"
    r"rd\s+/[sq]|>nul\s*2?&?\s*1?|--no-preserve-root|:\(\)\s*\{)", re.IGNORECASE)


def _load() -> dict:
    try:
        return json.load(open(REGISTRY, encoding="utf-8"))
    except (OSError, ValueError):
        return {"tools": [], "scanned_at": None}


def _save(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = REGISTRY + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY)


def _roots() -> list[str]:
    env = os.environ.get("LOCAL_TOOL_ROOTS")
    if env:
        return [r.strip() for r in env.split(";") if r.strip()]
    return _DEFAULT_ROOTS


def scan() -> dict:
    """扫描工具根 → 注册表（name = 可执行文件名小写去扩展）。"""
    tools = []
    for root in _roots():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not _EXCLUDE_DIR.search(d) and not d.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith(_EXTS):
                    continue
                if _EXCLUDE_NAME.search(fn):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) < 10_000:  # 太小的多半是 stub/链接
                        continue
                except OSError:
                    continue
                name = os.path.splitext(fn)[0].lower()
                tools.append({"name": name, "path": fp, "kind": os.path.splitext(fn)[1][1:],
                              "dir": os.path.basename(dirpath),
                              "size_kb": os.path.getsize(fp) // 1024})
    # 去重（同名保留第一个——按根顺序优先）
    seen: set = set()
    dedup = []
    for t in tools:
        if t["name"] in seen:
            continue
        seen.add(t["name"])
        dedup.append(t)
    dedup.sort(key=lambda t: t["name"])
    data = {"tools": dedup, "scanned_at": time.time()}
    _save(data)
    return {"ok": True, "count": len(dedup), "registry": REGISTRY,
            "tools": dedup}


def discover(query: str = "", category: str = "") -> dict:
    """列出已注册工具（query 名称子串过滤）。"""
    data = _load()
    tools = data.get("tools", [])
    if query:
        q = query.lower()
        tools = [t for t in tools if q in t["name"] or q in t["dir"].lower()]
    if category:
        tools = [t for t in tools if category.lower() in t["dir"].lower()]
    return {"ok": True, "count": len(tools), "registry": REGISTRY,
            "scanned_at": data.get("scanned_at"), "tools": tools}


def run(name: str, args: list[str] | None = None, timeout: int = 60) -> dict:
    """安全调用桥：白名单注册工具 + 危险参数黑名单 + 超时 + 输出截断。"""
    if not 1 <= timeout <= 600:
        return {"ok": False, "error": "timeout 须在 1..600 秒"}
    data = _load()
    match = next((t for t in data.get("tools", []) if t["name"] == name.lower()), None)
    if match is None:
        return {"ok": False, "error": f"工具未注册: {name}（先 scan() 或确认在工具根内）",
                "hint": f"可用 discover() 查看已注册工具（{len(data.get('tools', []))} 个）"}
    args = args or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return {"ok": False, "error": "args 须为字符串列表"}
    joined = " ".join(args)
    if _DANGEROUS.search(joined):
        return {"ok": False, "error": f"参数含危险操作被拒绝: {joined[:100]}"}
    cmd = [match["path"], *args]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        out = (p.stdout or "")[:20_000]
        err = (p.stderr or "")[:5000]
        return {"ok": True, "name": name, "path": match["path"],
                "exit_code": p.returncode, "stdout": out, "stderr": err,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "name": name, "error": f"超时（>{timeout}s）",
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    except OSError as e:
        return {"ok": False, "name": name, "error": str(e)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        print(json.dumps(scan(), ensure_ascii=False, indent=2)[:1500])
    else:
        print(json.dumps(discover(), ensure_ascii=False, indent=2)[:1500])
