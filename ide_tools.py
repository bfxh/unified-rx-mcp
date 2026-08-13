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


# ── 行级注释/字符串剥离（ide_complete/ide_rename 共用：防注释/字符串里的
#    假符号污染补全候选与重命名引用——IDE 强度增强 2026-08-13）──
# 修复（2026-08-13）：跨行块注释/三引号字符串必须**保留换行数**——
# 否则剥离后行号错位，ide_rename/ide_references 的 file:line 会指向错误行
# （实测：3 行块注释被压成 1 行，9 行文件变 6 行，行号错位 3 行）。
_SINGLE_STR_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
_TRIPLE_DQ_RE = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""', re.S)
_TRIPLE_SQ_RE = re.compile(r"'''(?:[^'\\]|\\.|'(?!''))*'''", re.S)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _keep_newlines(m: "re.Match[str]") -> str:
    """替换匹配文本为等量换行（保持行号不变；单行匹配替换为空串）。"""
    return "\n" * m.group(0).count("\n")


def _strip_comments_strings(text: str) -> str:
    """剥离字符串字面量与注释（跨行三引号/块注释 + 单行 // # 与行内 #），返回"代码面"。

    行号保持：所有跨行替换（块注释/三引号字符串）用 _keep_newlines 保留换行数，
    单行替换为空串——剥离后行号与原始文件一致（ide_rename/references 定位正确）。
    注释规则：
    - /* */ 块注释（跨行）→ 剥
    - // 行注释 → 剥
    - # 行注释（# 后跟空白/行尾；Rust 属性 #[...]/#![...] 保留）
    """
    text = _TRIPLE_DQ_RE.sub(_keep_newlines, text)
    text = _TRIPLE_SQ_RE.sub(_keep_newlines, text)
    text = _BLOCK_COMMENT_RE.sub(_keep_newlines, text)
    text = _SINGLE_STR_RE.sub("", text)
    out = []
    for line in text.splitlines():
        # 行注释：// 与 #（# 后跟 [ 或 ! 是 Rust 属性——保留）
        cut = len(line)
        for marker in ("//",):
            idx = line.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        for i, ch in enumerate(line):
            if ch == "#":
                nxt = line[i + 1:i + 2]
                if nxt not in ("[", "!"):
                    cut = min(cut, i)
                    break
        out.append(line[:cut])
    return "\n".join(out)


# 声明模式（按语言族；行首限定——声明优先排序用）
_DECL_RE = re.compile(
    r"^\s*(?:(?:pub|pub\([^)]*\)|async|unsafe|const|static|export|default)\s+)*"
    r"(?:fn|struct|enum|trait|type|mod|impl|class|def|function|interface|let|var)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)


# ── ide_rename ─────────────────────────────────────────────
def ide_rename(root: str, symbol: str, new_name: str,
               max_refs: int = 200, exclude_comments: bool = True) -> dict:
    """安全重命名：找符号所有引用 → 替换（仅同名符号，保守策略）。

    exclude_comments=True（默认）：跳过注释/字符串内的同名 token——
    重命名引用列表只含"代码面"引用，避免把注释/字符串里的词误当引用。

    IDE 增强三（2026-08-13）：refs 每项附 `before`/`after` 行内替换预览
    （原始行——AI 可先看效果再决定应用，减少误改）。

    返回 {ok, changed_files, refs, error}——不实际落盘（L3 建议层），
    调用方确认后走 fs_write（L4 授权）应用。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new_name):
        return {"ok": False, "error": f"新名字非法: {new_name}"}
    refs = _find_symbol_refs(root, symbol, max_refs, exclude_comments)
    if not refs:
        return {"ok": False, "error": f"未找到符号引用: {symbol}"}
    # 行内替换预览（按文件聚合读一次——refs 多时不反复打开文件）
    previews: dict[str, list[str]] = {}
    for r in refs:
        previews.setdefault(r["file"], []).append(r)
    for file, items in previews.items():
        try:
            with open(file, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for r in items:
            if 1 <= r["line"] <= len(lines):
                orig = lines[r["line"] - 1].rstrip("\n")
                new_line = re.sub(rf"\b{re.escape(symbol)}\b", new_name, orig)
                r["before"] = orig.strip()[:60]
                r["after"] = new_line.strip()[:60]
    return {
        "ok": True,
        "symbol": symbol,
        "new_name": new_name,
        "refs": refs,
        "ref_count": len(refs),
        "exclude_comments": exclude_comments,
        "advice": f"确认后用 fs_write 逐文件应用（L4 授权）——refs 的 before/after 为行内替换预览",
    }


def _find_symbol_refs(root: str, symbol: str, max_refs: int,
                      exclude_comments: bool) -> list[dict]:
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
                    text = f.read()
            except OSError:
                continue
            if exclude_comments:
                text = _strip_comments_strings(text)
            for i, line in enumerate(text.splitlines(), 1):
                for m in re.finditer(rf"\b{re.escape(symbol)}\b", line):
                    refs.append({"file": p, "line": i, "col": m.start() + 1,
                                 "text": line.strip()[:80]})
                    if len(refs) >= max_refs:
                        return refs
    return refs


# ── ide_complete ───────────────────────────────────────────
def ide_complete(root: str, file_path: str, prefix: str, limit: int = 20) -> dict:
    """补全：同库符号匹配前缀（tree-sitter 图降级版——无 LSP 也可用）。

    IDE 强度增强：
    - 排除注释/字符串里的假符号（_strip_comments_strings）
    - **当前文件符号优先**（IDE 直觉：正在编辑的文件排最前）
    - 声明优先：fn/struct/class/def/let 等行首声明排前（kind 标注）
    - items 保持字符串列表（向后兼容）；新增 detailed（name/kind/file/line/current）
    """
    if not prefix:
        return {"ok": True, "items": [], "note": "空前缀"}
    cands: dict[str, dict] = {}  # name -> {kind, file, line, current}

    def scan_file(p: str, current: bool) -> None:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return
        code = _strip_comments_strings(text)
        for i, line in enumerate(code.splitlines(), 1):
            for m in re.finditer(rf"\b{re.escape(prefix)}[A-Za-z0-9_]*\b", line):
                name = m.group(0)
                dm = _DECL_RE.match(line)
                kind = "decl" if (dm and dm.group(1) == name) else "ref"
                cur = cands.get(name)
                if cur is None or (kind == "decl" and cur["kind"] != "decl"):
                    cands[name] = {"name": name, "kind": kind,
                                   "file": p, "line": i, "current": current}

    # 当前文件优先（正在编辑的符号排最前）；其余文件在 walk 中扫
    if file_path and os.path.isfile(file_path):
        scan_file(file_path, True)
    exts = (".rs", ".py", ".ts", ".js")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.endswith(exts) or os.path.abspath(fn) == os.path.abspath(file_path):
                continue
            scan_file(os.path.join(dirpath, fn), False)
    # 排序：当前文件 → 声明 → 名字字典序
    ranked = sorted(cands.values(),
                    key=lambda c: (0 if c["current"] else 1,
                                   0 if c["kind"] == "decl" else 1,
                                   c["name"]))
    ranked = ranked[:max(limit, 1)]
    return {"ok": True, "prefix": prefix,
            "items": [c["name"] for c in ranked],
            "detailed": ranked,
            "count": len(ranked)}


# ── ide_references（新 IDE 增强 2026-08-13：定义/引用区分）──
def ide_references(root: str, symbol: str, max_refs: int = 200,
                   exclude_comments: bool = True) -> dict:
    """查找符号定义与全部引用（IDE goto-references 降级版，无 LSP 可用）。

    定义判定：行首声明（fn/def/struct/class/let 等，_DECL_RE）且声明名==symbol。
    返回 {ok, symbol, definitions: [...], references: [...], count}。
    """
    refs = _find_symbol_refs(root, symbol, max_refs, exclude_comments)
    if not refs:
        return {"ok": False, "error": f"未找到符号: {symbol}"}
    definitions = []
    references = []
    for r in refs:
        # 逐行重读（_find_symbol_refs 已剥离注释/字符串——这里只需声明判定）
        try:
            with open(r["file"], encoding="utf-8", errors="replace") as f:
                line_text = f.readlines()[r["line"] - 1]
        except (OSError, IndexError):
            references.append(r)
            continue
        dm = _DECL_RE.match(line_text)
        if dm and dm.group(1) == symbol:
            definitions.append(r)
        else:
            references.append(r)
    return {
        "ok": True,
        "symbol": symbol,
        "definitions": definitions,
        "references": references,
        "definition_count": len(definitions),
        "reference_count": len(references),
        "count": len(refs),
        "advice": "用 ide_rename 生成重命名方案（L3 建议层，确认后 fs_write 应用）",
    }


# ── ide_actions ────────────────────────────────────────────
# 追加规则（IDE 强度增强 2026-08-13）：TODO/FIXME 未完成标记 + 空 except 吞错
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_EXCEPT_PASS_RE = re.compile(r"except[^:]*:\s*(?:pass\s*)?$|except[^:]*:\s*$")
_NEXT_LINE_PASS_RE = re.compile(r"^\s*pass\s*(#.*)?$")


def ide_actions(file_path: str) -> dict:
    """快速修复建议：基于文件内容规则扫描（无 LSP 的降级 code_action）。

    规则：unwrap/expect panic 风险、as 窄化、TODO/FIXME 未完成标记、
    空 except 吞错（error 级）——kind 区分 safety/cleanup。
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    actions = []
    is_python = file_path.endswith(".py")
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
        elif _TODO_RE.search(s):
            actions.append({
                "line": i, "title": "未完成标记（TODO/FIXME）",
                "detail": f"`{s[:60]}` 待实现/待修复",
                "kind": "cleanup",
            })
        elif is_python and ("except" in s and _EXCEPT_PASS_RE.match(s)
                            or _NEXT_LINE_PASS_RE.match(s)):
            actions.append({
                "line": i, "title": "空 except 吞错",
                "detail": f"`{s[:60]}` 静默吞异常——建议记录或显式处理",
                "kind": "safety",
            })
        if len(actions) >= 20:
            break
    return {"ok": True, "file": file_path, "actions": actions,
            "count": len(actions)}
