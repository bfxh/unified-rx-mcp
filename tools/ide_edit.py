# -*- coding: utf-8 -*-
"""tools/ide_edit.py —— 编辑面（S48 拆分）。"""
import os
import re

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.ide_common import (_read, _lang_of, _iter_files,
                              _detect_eol, MAX_CTX)

@tool("locate_edit", "定位：符号/关键词 → file:line + snippet（改代码引导）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "代码库根目录"},
           "query": {"type": "string", "description": "要改的符号/关键词/报错片段"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
           "limit": {"type": "integer", "description": "候选数（默认 10）"},
       },
       "required": ["path", "query"]})
def locate_edit(path, query, max_files=100, limit=10):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    query = query.strip()
    if not query:
        # S7 攻击修复：空/纯空白查询会把全库前 N 行当"命中"返回（total=15/refs=12424 纯噪音）
        # 结构化失败（错误进 result.error 而非 ok 层），调用方语义一致
        return {"error": "query 为空——请提供符号或关键词"}
    hits = []
    all_sources = {}  # S6-D2: 引用计数需要全量文件内容（max_files 范围内）
    for fp in _iter_files(path, max_files):
        src = _read(fp)
        if not src:
            continue
        all_sources[fp] = src
        lines = src.split("\n")
        for idx, line in enumerate(lines, 1):
            # 符号/关键词命中（区分大小写优先精确，其次忽略大小写）
            if query in line or (query.lower() in line.lower() and query not in line):
                ctx = lines[max(0, idx - 2):idx + 3]
                hits.append({"file": fp, "line": idx, "snippet": "\n".join(ctx)})
                if len(hits) >= limit * 3:
                    break
        if len(hits) >= limit * 3:
            break
    # S6-D2 影响面事实：query 在扫描范围内的出现总次数。
    # 只提供计数事实，不判断该不该改——影响面决策留给智能体。
    ref_count = sum(src.count(query) for src in all_sources.values())
    return {"query": query, "total": len(hits), "references_in_scan": ref_count,
            "hits": hits[:limit]}

@tool("code_context", "读取光标附近代码上下文（改前取上下文）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件绝对路径"},
           "cursor_line": {"type": "integer", "description": "光标行号（1-based，0=无）"},
           "radius": {"type": "integer", "description": "半径行数（默认 30）"},
       },
       "required": ["path"]})
def code_context(path, cursor_line=0, radius=30):
    src = _read(path)
    if src is None:
        return {"error": f"文件不可读: {path}"}
    lines = src.split("\n")
    radius = max(5, min(int(radius or 30), 200))
    if not cursor_line:
        start, end = 0, min(len(lines), 80)
    else:
        start = max(0, cursor_line - 1 - radius)
        end = min(len(lines), cursor_line - 1 + radius)
    return {
        "file": path, "lang": _lang_of(path),
        "total_lines": len(lines),
        "start": start + 1, "end": end,
        "content": "\n".join(lines[start:end]),
    }

@tool("ide_edit_multi", "多行修改：内容匹配应用（支持 occ 指定第几次出现；保留原行尾）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "仓库根（可选）"},
           "file_path": {"type": "string", "description": "文件"},
           "edits": {"type": "array",
                     "description": "[{old_lines: [...], new_lines: [...], occ?: 1}]——old_lines 逐行精确匹配；occ 指定匹配第几次出现（默认 1）"},
       },
       "required": ["file_path", "edits"]},
      requires_auth=True)
def ide_edit_multi(file_path, edits, root=None, __authorized=False, dry_run=False):
    # S44 ponytail：old_lines/new_lines 顶层兼容参数砍除（全仓无调用方，edits 内用法不变）
    p = file_path
    if root and not os.path.isabs(p):
        p = os.path.join(root, p)
    try:
        p = _fs_resolve(p)
    except ValueError as e:
        return {"error": str(e)}
    src = _read(p)
    if src is None:
        return {"error": f"文件不可读: {p}"}
    eol = _detect_eol(src)
    lines = src.split("\n")
    # 去掉每行尾部的 \r（CRLF 时），统一成 \n 数组
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    applied = 0
    errors = []
    edits = edits or []
    # S34：先整段模拟匹配（在副本上），失败不写盘——dry_run 也复用同一模拟
    sim = list(lines)
    sim_errors = []
    for e in edits:
        old = e.get("old_lines") or []
        new = e.get("new_lines") or []
        occ = int(e.get("occ", 1) or 1)
        if not old:
            sim_errors.append("old_lines 为空")
            continue
        found = -1
        seen = 0
        for i in range(len(sim) - len(old) + 1):
            if sim[i:i + len(old)] == old:
                seen += 1
                if seen == occ:
                    found = i
                    break
        if found < 0:
            sim_errors.append(f"未匹配(occ={occ}): {old[0][:60]!r}...")
            continue
        sim[found:found + len(old)] = new
        applied += 1
    if applied == 0:
        return {"error": f"0 应用: {sim_errors[:3]}", "applied": 0, "errors": sim_errors}
    out = eol.join(sim)
    if dry_run:
        # S34：预览模式——unified diff，不落盘（887 次调用里预览是高频需求）
        import difflib
        diff = "".join(difflib.unified_diff(
            src.splitlines(keepends=True), out.splitlines(keepends=True),
            fromfile=file_path, tofile=file_path + " (dry_run)"))
        return {"applied": applied, "errors": sim_errors, "file": p,
                "dry_run": True, "diff": diff[:MAX_CTX]}
    # 写回：保留原行尾（I3 修复）
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    return {"applied": applied, "errors": sim_errors, "file": p,
            "eol": "CRLF" if eol == "\r\n" else "LF"}

@tool("ide_rename", "安全重命名：全库找引用→建议（L3 不落盘）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string"},
           "symbol": {"type": "string"},
           "new_name": {"type": "string"},
           "include_plan": {"type": "boolean", "description": "生成批量应用计划（默认 false）"},
       },
       "required": ["root", "symbol", "new_name"]})
def ide_rename(root, symbol, new_name, include_plan=False):
    try:
        root = _fs_resolve(root)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    plan = []
    for fp in _iter_files(root, 200):
        src = _read(fp)
        if not src:
            continue
        if symbol in src:
            plan.append({"file": fp, "occurrences": src.count(symbol)})
    return {
        "symbol": symbol, "new_name": new_name,
        "files_affected": len(plan), "total_occurrences": sum(p["occurrences"] for p in plan),
        "plan": plan if include_plan else None,
        "note": "L3 只建议不落盘；确认后可用 fs_write 应用",
    }
