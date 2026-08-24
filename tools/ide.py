# -*- coding: utf-8 -*-
"""tools/ide.py —— IDE 增强域（6 工具）

收敛自旧版 ide_complete_chain/ide_continue/ide_jump_predict/ide_open_at 等。
重点修复旧版 0 应用问题：ide_edit_multi 用「内容匹配」而非「行号匹配」，
行号偏移不再导致编辑静默失败。
2026-08-25 修复（用户反馈 IDE 限制 AI）：
- I1: ide_edit_multi 支持 occ 参数（同内容多处，指定第几次出现）
- I2: 行数组块匹配（消除拼接字符串 find 的顺序依赖隐患）
- I3: 写回保留原行尾（CRLF/LF 不破坏）
- I4: locate_edit/ide_references 等 max_files 只计代码文件
"""
import os
import re

from registry import tool

MAX_CTX = 5000

_SKIP_DIRS = (".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".unified-rx-index", ".codegraph", "backups", "assets", "data", "models", "docs")


def _read(path):
    if not os.path.isfile(path):
        return None
    # newline="" 保留原始行尾（CRLF 不被转 LF），供 _detect_eol 检测
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {"py": "python", "rs": "rust", "go": "go", "ts": "typescript",
            "tsx": "typescript", "js": "javascript", "jsx": "javascript",
            "gd": "gdscript", "cs": "csharp", "dart": "dart"}.get(ext.lstrip("."), "text")


def _iter_files(root, max_files, skip_dirs=None):
    """遍历代码文件：max_files 只计有语言的代码文件（I4 修复）。"""
    skip = set(_SKIP_DIRS)
    if skip_dirs:
        skip |= set(skip_dirs)
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            fp = os.path.join(r, fn)
            if _lang_of(fp) == "text":
                continue
            if count >= max_files:
                return
            count += 1
            yield fp


def _detect_eol(src):
    """检测行尾：CRLF / LF。"""
    crlf = src.count("\r\n")
    lf = src.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


# ---------- locate_edit：自然语言/符号 → 位置 ----------
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
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    query = query.strip()
    hits = []
    for fp in _iter_files(path, max_files):
        src = _read(fp)
        if not src:
            continue
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
    return {"query": query, "total": len(hits), "hits": hits[:limit]}


# ---------- code_context：光标上下文 ----------
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


# ---------- ide_edit_multi：内容匹配多行编辑（核心修复） ----------
@tool("ide_edit_multi", "多行修改：内容匹配应用（支持 occ 指定第几次出现；保留原行尾）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "仓库根（可选）"},
           "file_path": {"type": "string", "description": "文件"},
           "edits": {"type": "array",
                     "description": "[{old_lines: [...], new_lines: [...], occ?: 1}]——old_lines 逐行精确匹配；occ 指定匹配第几次出现（默认 1）"},
       },
       "required": ["file_path", "edits"]})
def ide_edit_multi(file_path, edits, root=None):
    p = file_path
    if root and not os.path.isabs(p):
        p = os.path.join(root, p)
    src = _read(p)
    if src is None:
        return {"error": f"文件不可读: {p}"}
    eol = _detect_eol(src)
    lines = src.split("\n")
    # 去掉每行尾部的 \r（CRLF 时），统一成 \n 数组
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    applied = 0
    errors = []
    for e in edits or []:
        old = e.get("old_lines") or []
        new = e.get("new_lines") or []
        occ = int(e.get("occ", 1) or 1)
        if not old:
            errors.append("old_lines 为空")
            continue
        # 逐行块匹配（第 occ 次出现）——I1/I2 修复
        found = -1
        seen = 0
        for i in range(len(lines) - len(old) + 1):
            if lines[i:i + len(old)] == old:
                seen += 1
                if seen == occ:
                    found = i
                    break
        if found < 0:
            errors.append(f"未匹配(occ={occ}): {old[0][:60]!r}...")
            continue
        lines[found:found + len(old)] = new
        applied += 1
    if applied == 0:
        return {"error": f"0 应用: {errors[:3]}", "applied": 0, "errors": errors}
    # 写回：保留原行尾（I3 修复）
    out = eol.join(lines)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    return {"applied": applied, "errors": errors, "file": p, "eol": "CRLF" if eol == "\r\n" else "LF"}


# ---------- ide_references：符号引用查找 ----------
@tool("ide_references", "查找符号定义与引用（文本级）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "代码库根目录"},
           "symbol": {"type": "string"},
       },
       "required": ["root", "symbol"]})
def ide_references(root, symbol):
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    defs, refs = [], []
    for fp in _iter_files(root, 200):
        src = _read(fp)
        if not src:
            continue
        for idx, line in enumerate(src.split("\n"), 1):
            if symbol not in line:
                continue
            item = {"file": fp, "line": idx, "text": line.strip()[:100]}
            # 定义启发：def/fn/func/class/struct/const/let + 符号
            if re.search(rf"\b(def|fn|func|class|struct|enum|const|let|pub)\s+{re.escape(symbol)}\b", line):
                defs.append(item)
            else:
                refs.append(item)
    return {"symbol": symbol, "defs": defs[:10], "refs": refs[:30], "total": len(defs) + len(refs)}


# ---------- ide_rename：安全重命名（建议不落盘） ----------
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


# ---------- code_complete：符号补全（文本级） ----------
@tool("code_complete", "符号补全：声明优先（文本级，无 LSP）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string"},
           "file": {"type": "string", "description": "当前文件"},
           "prefix": {"type": "string", "description": "补全前缀"},
       },
       "required": ["root", "prefix"]})
def code_complete(root, prefix, file=None):
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    decls, others = set(), set()
    for fp in _iter_files(root, 100):
        src = _read(fp)
        if not src:
            continue
        # 前缀开头匹配（I5 修复：counter 匹配 count）
        for m in re.finditer(rf"\b{re.escape(prefix)}[A-Za-z0-9_]*\b", src):
            name = m.group(0)
            line = src[:m.start()].count("\n") + 1
            line_text = src.split("\n")[line - 1]
            if re.search(rf"\b(def|fn|func|class|struct|enum|const|let)\b.*{name}", line_text):
                decls.add(name)
            else:
                others.add(name)
    suggestions = sorted(decls)[:10] + sorted(others - decls)[:10]
    return {"prefix": prefix, "suggestions": suggestions, "total": len(suggestions)}
