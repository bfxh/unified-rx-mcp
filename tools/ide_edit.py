# -*- coding: utf-8 -*-
"""tools/ide_edit.py —— 编辑面（S48 拆分；R1 写前防护）。

写前两道门：
- 语法门（默认开，仅 .py）：编辑结果 ast.parse 编译失败 → 整批拒绝不落盘
  （诚实定界：rust/go 无 stdlib 语法器，不假装支持）
- LSP 验证（validate=true 时）：未落盘内容推 LSP 泵诊断，有 error 拒写；
  LSP 不可用时如实降级放行（不挡编辑）
"""
import ast
import os
import re

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.ide_common import (_read, _lang_of, _iter_files,
                              _detect_eol, MAX_CTX)
from tools.lsp import validate_content

_MAX_EDIT_BYTES = 10 * 1024 * 1024   # S61：超过即拒编辑——读截断+写回=静默丢内容

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
    try:
        if os.path.getsize(path) > _MAX_EDIT_BYTES:
            return {"error": f"文件超过 {_MAX_EDIT_BYTES // (1024 * 1024)}MB——拒绝读取"}
    except OSError:
        pass
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

def _match_idx(hay, needle, occ, fuzzy):
    """块匹配：精确（逐行全等）或空白容忍（strip 后比对，行首缩进差异也容忍）。
    返回第 occ 次出现的下标，无则 -1。fuzzy 只放宽【查找】，new_lines 仍按
    调用方给的落盘（缩进由调用方负责）。"""
    if fuzzy:
        hay = [l.strip() for l in hay]
        needle = [l.strip() for l in needle]
    seen = 0
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            seen += 1
            if seen == occ:
                return i
    return -1


@tool("ide_edit_multi", "多行修改：内容匹配应用（支持 occ 指定第几次出现；保留原行尾；"
      "fuzzy=空白容忍查找）；py 写前语法门默认开，validate=true 再加 LSP 写前验证", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "仓库根（可选）"},
           "file_path": {"type": "string", "description": "文件"},
           "edits": {"type": "array",
                     "description": "[{old_lines: [...], new_lines: [...], occ?: 1}]——old_lines 逐行精确匹配；occ 指定匹配第几次出现（默认 1）"},
           "validate": {"type": "boolean",
                        "description": "LSP 写前验证（error 拒写；LSP 不可用如实放行）"},
           "fuzzy": {"type": "boolean",
                     "description": "空白容忍匹配（rstrip 比对；new_lines 原样落盘，缩进由调用方负责）"},
       },
       "required": ["file_path", "edits"]},
      requires_auth=True)
def ide_edit_multi(file_path, edits, root=None, __authorized=False,
                   dry_run=False, validate=False, fuzzy=False):
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
    # S60：BOM 文件内容匹配修复——\ufeff 在行首导致 old_lines 永不匹配
    had_bom = src.startswith("\ufeff")
    if had_bom:
        src = src[1:]
    eol = _detect_eol(src)
    # S61：尺寸护栏——超大文件拒绝编辑（读截断+写回=静默丢内容，宁可拒）
    try:
        if os.path.getsize(p) > _MAX_EDIT_BYTES:
            return {"error": f"文件超过编辑上限 {_MAX_EDIT_BYTES // (1024 * 1024)}MB"
                             "——拒绝（防截断静默丢内容）"}
    except OSError:
        pass
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
        found = _match_idx(sim, old, occ, fuzzy=False)
        if found < 0 and fuzzy:
            found = _match_idx(sim, old, occ, fuzzy=True)
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
    # R1 语法门（默认开，仅 .py）：双侧可编译性——原文件可解析才要求结果可解析；
    # 原文件本就解析失败（BOM/编码损伤/已损坏）→ 跳过门，不让假阳性挡编辑
    if os.path.splitext(p)[1].lower() == ".py":
        src_parses = True
        try:
            ast.parse(src)
        except SyntaxError:
            src_parses = False
        if src_parses:
            try:
                ast.parse(out)
            except SyntaxError as e:
                return {"error": f"语法门: 编辑结果第 {e.lineno} 行无法编译 "
                                 f"({e.msg})——未落盘", "applied": 0}
    # R1 LSP 写前验证（显式开启时）
    validation = None
    if validate:
        v = validate_content(p, out)
        if v.get("error"):
            validation = {"skipped": v["error"]}       # LSP 不可用如实放行
        elif v.get("errors"):
            return {"error": f"写前验证: LSP 报告 {v['errors']} 个 error——未落盘",
                    "applied": 0, "validation": v}
        else:
            validation = {"ok": True, "engine": v.get("engine"),
                          "total": v.get("total")}
    # 写回：保留原行尾（I3 修复）+ BOM 还原
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(("\ufeff" + out) if had_bom else out)
    result = {"applied": applied, "errors": sim_errors, "file": p,
              "eol": "CRLF" if eol == "\r\n" else "LF"}
    if validation is not None:
        result["validation"] = validation
    return result

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
