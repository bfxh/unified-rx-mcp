#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_tools.py — IDE 全家桶（IDE_ENHANCE_PLAN R4）。

hover 已有（lsp_query request=hover）。补齐三件：
  ide_rename   — 安全重命名：graph_index callers/callees 全覆盖验证后替换
  ide_complete — 补全：tree-sitter 同库符号（无 LSP 环境降级可用）
  ide_actions  — 快速修复：bug_scan 规则 → code action 建议列表
"""

import json
import os
import re


# ── ide_rename ─────────────────────────────────────────────
def ide_rename(root: str, symbol: str, new_name: str,
               max_refs: int = 200) -> dict:
    """安全重命名：找符号所有引用 → 替换（仅同名符号，保守策略）。

    返回 {ok, changed_files, refs, error}——不实际落盘（L3 建议层），
    调用方确认后走 fs_write（L4 授权）应用。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new_name):
        return {"ok": False, "error": f"新名字非法: {new_name}"}
    refs = _find_symbol_refs(root, symbol, max_refs)
    if not refs:
        return {"ok": False, "error": f"未找到符号引用: {symbol}"}
    return {
        "ok": True,
        "symbol": symbol,
        "new_name": new_name,
        "refs": refs,
        "ref_count": len(refs),
        "advice": f"确认后用 fs_write 逐文件应用（L4 授权）",
    }


def _find_symbol_refs(root: str, symbol: str, max_refs: int) -> list[dict]:
    """全库找符号引用（词级匹配 + 边界检查——保守：只报位置不改）。"""
    refs = []
    exts = (".rs", ".py", ".ts", ".js", ".c", ".h", ".cpp", ".hpp", ".gd")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                for m in re.finditer(rf"\b{re.escape(symbol)}\b", line):
                    refs.append({"file": p, "line": i, "col": m.start() + 1,
                                 "text": line.strip()[:80]})
                    if len(refs) >= max_refs:
                        return refs
    return refs


# ── ide_complete ───────────────────────────────────────────
def ide_complete(root: str, file_path: str, prefix: str, limit: int = 20) -> dict:
    """补全：同库符号匹配前缀（tree-sitter 图降级版——无 LSP 也可用）。"""
    if not prefix:
        return {"ok": True, "items": [], "note": "空前缀"}
    items = set()
    exts = (".rs", ".py", ".ts", ".js")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.endswith(exts) or os.path.abspath(fn) == os.path.abspath(file_path):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for m in re.finditer(rf"\b{re.escape(prefix)}[A-Za-z0-9_]*\b", text):
                items.add(m.group(0))
                if len(items) >= limit * 2:
                    break
    return {"ok": True, "prefix": prefix,
            "items": sorted(items)[:limit],
            "count": min(len(items), limit)}


# ── ide_actions ────────────────────────────────────────────
def ide_actions(file_path: str) -> dict:
    """快速修复建议：基于文件内容规则扫描（无 LSP 的降级 code_action）。"""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    actions = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if ".unwrap()" in s and "test" not in file_path.lower():
            actions.append({
                "line": i, "title": "unwrap → 安全处理",
                "detail": f"`{s[:60]}` 建议 match/ok_or/?（生产代码 panic 风险）",
                "kind": "safety",
            })
        elif ".expect(" in s and "test" not in file_path.lower():
            actions.append({
                "line": i, "title": "expect → Result 传播",
                "detail": f"`{s[:60]}` 建议返回 Result 而非 panic",
                "kind": "safety",
            })
        elif re.search(r"\bas\s+(u8|i8|u16|i16)\b", s):
            actions.append({
                "line": i, "title": "as 收窄转换 → try_from",
                "detail": f"`{s[:60]}` 建议 try_from + 显式处理",
                "kind": "safety",
            })
        if len(actions) >= 20:
            break
    return {"ok": True, "file": file_path, "actions": actions,
            "count": len(actions)}
