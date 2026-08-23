# -*- coding: utf-8 -*-
"""tools/ide.py —— IDE 增强域（6 工具）

收敛自旧版 ide_complete_chain/ide_continue/ide_jump_predict/ide_open_at 等。
重点修复旧版 0 应用问题：ide_edit_multi 用「内容匹配」而非「行号匹配」，
行号偏移不再导致编辑静默失败。
"""
import os
import re

from registry import tool

MAX_CTX = 5000


def _read(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {"py": "python", "rs": "rust", "go": "go", "ts": "typescript",
            "tsx": "typescript", "js": "javascript", "jsx": "javascript",
            "gd": "gdscript", "cs": "csharp", "dart": "dart"}.get(ext.lstrip("."), "text")


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
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build", ".unified-rx-index")]
        for fn in files:
            if count >= max_files:
                break
            count += 1
            if not _lang_of(os.path.join(root, fn)):
                continue
            fp = os.path.join(root, fn)
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
@tool("ide_edit_multi", "多行修改：diff 格式输入→应用（内容匹配，非行号）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "仓库根（可选）"},
           "file_path": {"type": "string", "description": "文件"},
           "edits": {"type": "array",
                     "description": "[{old_lines: [...], new_lines: [...]}]——old_lines 必须与文件内容逐行精确匹配"},
       },
       "required": ["file_path", "edits"]})
def ide_edit_multi(file_path, edits, root=None):
    p = file_path
    if root and not os.path.isabs(p):
        p = os.path.join(root, p)
    src = _read(p)
    if src is None:
        return {"error": f"文件不可读: {p}"}
    lines = src.split("\n")
    applied = 0
    errors = []
    for e in edits or []:
        old = e.get("old_lines") or []
        new = e.get("new_lines") or []
        old_text = "\n".join(old)
        # 内容匹配（整块匹配，含行尾；兼容 CRLF）
        src_text = "\n".join(lines)
        idx = src_text.find(old_text)
        if idx < 0:
            errors.append(f"未匹配: {old_text[:60]!r}...")
            continue
        # 定位到行边界
        line_start = src_text.count("\n", 0, idx)
        line_end = line_start + len(old)
        lines[line_start:line_end] = new
        applied += 1
    if applied == 0:
        return {"error": f"0 应用: {errors[:3]}", "applied": 0, "errors": errors}
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return {"applied": applied, "errors": errors, "file": p}


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
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build")]
        for fn in files:
            if count >= 200:
                break
            count += 1
            if not _lang_of(os.path.join(r, fn)):
                continue
            fp = os.path.join(r, fn)
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
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build")]
        for fn in files:
            if count >= 200:
                break
            count += 1
            if not _lang_of(os.path.join(r, fn)):
                continue
            fp = os.path.join(r, fn)
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
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build")]
        for fn in files:
            if count >= 100:
                break
            count += 1
            if not _lang_of(os.path.join(r, fn)):
                continue
            src = _read(os.path.join(r, fn))
            if not src:
                continue
            for m in re.finditer(rf"\b([A-Za-z_][A-Za-z0-9_]*{re.escape(prefix)}[A-Za-z0-9_]*)\b", src):
                name = m.group(1)
                line = src[:m.start()].count("\n") + 1
                line_text = src.split("\n")[line - 1]
                if re.search(rf"\b(def|fn|func|class|struct|enum|const|let)\b.*{name}", line_text):
                    decls.add(name)
                else:
                    others.add(name)
    suggestions = sorted(decls)[:10] + sorted(others - decls)[:10]
    return {"prefix": prefix, "suggestions": suggestions, "total": len(suggestions)}
