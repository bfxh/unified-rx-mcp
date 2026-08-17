#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/termlog.py — 内建命令执行（ide_commands 手册）+ 扫描日志尾部（dashboard）。"""
import json
import os
import re

from ide_commands import _CHEATSHEET, local_run
from dashboard import _read_jsonl

LOG_FILE = os.path.join(os.path.expanduser("~"), ".unified-rx", "scan-log.jsonl")
_TAIL_CAP = 200  # 单次尾取条数上限（防刷屏）
_CHUNK = 65536   # dashboard._read_jsonl 回读块大小（小文件首行截断边界）


def _log_path() -> str:
    """日志路径：默认 ~/.unified-rx/scan-log.jsonl（可被环境变量覆盖）。"""
    override = os.environ.get("UNIFIED_RX_SCAN_LOG", "")
    return override if override.strip() else LOG_FILE


def _fmt_rec(rec: dict) -> str:
    """一条记录 → 紧凑一行 `ts level tool msg`（level=OK/ERR）。"""
    ts = str(rec.get("ts") or "")
    level = "OK " if rec.get("ok", True) else "ERR"
    tool = str(rec.get("tool") or "?")
    msg = str(rec.get("summary") or "")[:120]
    return f"{ts} {level} {tool} {msg}".rstrip()


def _tail_lines(path: str, count: int) -> list[dict]:
    """直读尾部 count 条（旧在前）——小文件首行截断兜底（_read_jsonl 按块回读，
    尾部不足一块时块首残留行会被切掉丢最旧一行）。"""
    recs: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f if ln.strip()]
    for ln in lines[-count:]:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            pass
    return recs


def _match(text: str):
    """命令匹配：前两 token 作 domain/name，或首 token 带 `/` 的
    domain/name（与 available 列表展示形式一致）；否则全域按 name 找。

    命中返回 (domain, entry, 剩余 token)；未命中 None。
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    head = tokens[0]
    if "/" in head:  # 斜杠写法 git/status ≡ 空格写法 git status
        domain, _, name = head.partition("/")
        entry = next((c for c in _CHEATSHEET.get(domain, [])
                      if c["name"] == name), None)
        if entry is not None:
            return domain, entry, tokens[1:]
    if len(tokens) >= 2 and head in _CHEATSHEET:
        entry = next((c for c in _CHEATSHEET[head]
                      if c["name"] == tokens[1]), None)
        if entry is not None:
            return head, entry, tokens[2:]
    for domain, cmds in _CHEATSHEET.items():
        entry = next((c for c in cmds if c["name"] == head), None)
        if entry is not None:
            return domain, entry, tokens[1:]
    return None


def run_command(text: str, workdir: str | None = None) -> dict:
    """执行 `>` 命令：匹配 _CHEATSHEET → 剩余 token 按占位符顺序填 → local_run。"""
    hit = _match(text)
    if hit is None:
        return {"ok": False, "error": "未知命令",
                "available": [f"{d}/{c['name']}" for d, cmds in _CHEATSHEET.items()
                              for c in cmds][:20]}
    domain, entry, rest = hit
    args: dict[str, str] = {}
    for ph in re.findall(r"{(\w+)}", entry["cmd"]):  # 占位符按出现顺序填
        if ph not in args and rest:
            args[ph] = rest.pop(0)
    return local_run(domain, entry["name"], args, workdir)


def log_tail(cursor: int = 0) -> dict:
    """扫描日志尾部：cursor=已读条数，返回之后的新条目（紧凑一行/条）。

    每条格式 `ts level tool msg`；文件不存在返回空。
    """
    path = _log_path()
    if not os.path.exists(path):
        return {"lines": [], "cursor": cursor}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            total = sum(1 for ln in f if ln.strip())
    except OSError:
        return {"lines": [], "cursor": cursor}
    if cursor >= total:
        return {"lines": [], "cursor": total}
    need = min(total - cursor, _TAIL_CAP)
    if size < _CHUNK:
        # 尾部小于一个回读块：_read_jsonl 会丢最旧一行 → 直读兜底
        recs = _tail_lines(path, need)
    else:
        recs = list(reversed(_read_jsonl(path, need)))  # 新在前 → 翻回时间序
        if len(recs) < need:  # 块边界截断兜底
            recs = _tail_lines(path, need)
    return {"lines": [_fmt_rec(r) for r in recs], "cursor": total}
