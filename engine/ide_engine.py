import sys as _sys
for _m in ['ide_tools', 'ide_ui', 'ide_session', 'ide_commands', 'ide_cache', 'ide_fusion', 'rx_ide']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])



"""ide_engine — IDE 引擎（合并自 7 个 IDE 模块：tools/ui/session/commands/cache/fusion/rx）。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""

# ══════════════ ide_tools（合并） ══════════════
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

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
               max_refs: int = 200, exclude_comments: bool = True,
               include_plan: bool = False) -> dict:
    """安全重命名：找符号所有引用 → 替换（仅同名符号，保守策略）。

    exclude_comments=True（默认）：跳过注释/字符串内的同名 token——
    重命名引用列表只含"代码面"引用，避免把注释/字符串里的词误当引用。

    IDE 增强三：refs 每项附 `before`/`after` 行内替换预览。
    IDE 增强六（2026-08-13）：include_plan=True 时生成 `apply_plan`——
    按文件聚合的行级编辑列表（fs_write 就绪），AI 确认后 L4 授权一步应用，
    无需手工逐处构造替换。

    返回 {ok, changed_files, refs, error}——不实际落盘（L3 建议层），
    调用方确认后走 fs_write（L4 授权）应用。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new_name):
        return {"ok": False, "error": f"新名字非法: {new_name}"}
    refs = _find_symbol_refs(root, symbol, max_refs, exclude_comments)
    if not refs:
        return {"ok": False, "error": f"未找到符号引用: {symbol}"}
    # 行内替换预览 + 应用计划（按文件聚合读一次——refs 多时不反复打开文件）
    previews: dict[str, list[str]] = {}
    for r in refs:
        previews.setdefault(r["file"], []).append(r)
    apply_plan: dict[str, list[dict]] = {}
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
                if include_plan:
                    apply_plan.setdefault(file, []).append({
                        "line": r["line"],
                        "old": orig,
                        "new": new_line,
                    })
    result = {
        "ok": True,
        "symbol": symbol,
        "new_name": new_name,
        "refs": refs,
        "ref_count": len(refs),
        "exclude_comments": exclude_comments,
        "advice": f"确认后用 fs_write 逐文件应用（L4 授权）——refs 的 before/after 为行内替换预览",
    }
    if include_plan:
        result["apply_plan"] = apply_plan
        result["plan_files"] = len(apply_plan)
        result["plan_edits"] = sum(len(v) for v in apply_plan.values())
        result["apply_hint"] = (
            "应用方式：对 apply_plan 中每个文件，读取原文后用 new 行替换对应 "
            "line 的 old 行，再 fs_write 整文件（L4 授权：参数加 __authorized: true）。"
            "建议应用后跑测试回归确认。"
        )
    return result


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
# 标识符模式（Rust/Python/TS/JS 通用）
_IDENT_RE = r"[A-Za-z_][A-Za-z0-9_]*"


def ide_complete(root: str, file_path: str, prefix: str, limit: int = 20,
                 match: str = "auto", sort: str = "line") -> dict:
    """补全：同库符号匹配前缀（tree-sitter 图降级版——无 LSP 也可用）。

    IDE 增强五（2026-08-13）：
    - **子串降级**：match="auto"（默认）前缀命中不足时自动子串匹配
      （输入 "rea" 也能补出 computeArea）；"prefix"/"substring" 可强制
    - **符号热度排序**：引用次数多的符号排前（在 当前文件/声明 之后）
    - 排除注释/字符串里的假符号；当前文件符号优先；detailed 加 refs（热度）
    - items 保持字符串列表（向后兼容）
    """
    if not prefix:
        # 清单 C（IDE 增强九十三）：空前缀 → 声明符号浏览（补全体验——
        # 输入空也能看到库里有什么符号，按行号/名字排序取 limit）
        # IDE 增强九十四：文件数上限（防大仓库读全库耗 token——浏览够用即可，
        # 达到上限提前退出并标记 truncated）
        exts = (".rs", ".py", ".ts", ".js", ".c", ".h", ".cpp", ".hpp", ".gd")
        decls: dict[str, dict] = {}
        _files_scanned = 0
        _MAX_BROWSE_FILES = 200
        _browse_truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("target", "node_modules", ".git", "release")]
            for fn in filenames:
                if not fn.endswith(exts):
                    continue
                p = os.path.join(dirpath, fn)
                _files_scanned += 1
                if _files_scanned > _MAX_BROWSE_FILES:
                    _browse_truncated = True
                    break
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    m = _DECL_RE.match(line)
                    if m:
                        name = m.group(1)
                        if name not in decls:
                            decls[name] = {"name": name, "kind": "decl",
                                           "file": p, "line": i}
            if _browse_truncated:
                break
        # IDE 增强 131：browse 排序参数（sort=line 按行号 / sort=name 按名字）
        if sort == "name":
            ranked = sorted(decls.values(),
                            key=lambda c: (c["name"], c["line"]))[:max(limit, 1)]
        else:
            ranked = sorted(decls.values(),
                            key=lambda c: (c["line"], c["name"]))[:max(limit, 1)]
        return {"ok": True, "prefix": "", "items": [c["name"] for c in ranked],
                "detailed": ranked, "count": len(ranked),
                "match_mode": "browse",
                "truncated": _browse_truncated,
                "note": "空前缀 → 声明符号浏览（库里有什么一目了然；"
                        f"大仓库最多扫 {_MAX_BROWSE_FILES} 个文件防耗 token）"}
    prefix_re = re.compile(rf"\b{re.escape(prefix)}{_IDENT_RE}")
    # 子串匹配：先匹配"含 prefix 的标识符片段"（[A-Za-z0-9_]* 允许 _ 前导——
    # compute_area 中 area 前是 _），再向左右扩展成完整符号名（防 \b 挡中间子串、
    # 防取到 _area 这类半截名）
    substring_frag_re = re.compile(rf"[A-Za-z0-9_]*{re.escape(prefix)}[A-Za-z0-9_]*")
    _IDENT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"

    def _expand_ident(line: str, start: int, end: int) -> str:
        while start > 0 and line[start - 1] in _IDENT_CHARS:
            start -= 1
        while end < len(line) and line[end] in _IDENT_CHARS:
            end += 1
        return line[start:end]

    cands: dict[str, dict] = {}  # name -> {kind, file, line, current, prefix_match, refs}

    def _record_decl(name: str, line: int, p: str, current: bool,
                     is_prefix: bool, decl_name: str | None) -> None:
        cur = cands.get(name)
        if cur is None:
            kind = "decl" if decl_name == name else "ref"
            cands[name] = {"name": name, "kind": kind, "file": p, "line": line,
                           "current": current, "prefix_match": is_prefix, "refs": 1}
        else:
            cur["refs"] += 1
            if is_prefix and not cur["prefix_match"]:
                cur["prefix_match"] = True
            if decl_name == name and cur["kind"] != "decl":
                cur["kind"] = "decl"

    # 声明优先判定需要行内容——scan_file_full 内联声明检测（单次读文件）
    def scan_file_full(p: str, current: bool) -> None:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return
        code = _strip_comments_strings(text)
        for i, line in enumerate(code.splitlines(), 1):
            dm = _DECL_RE.match(line)
            decl_name = dm.group(1) if dm else None
            if match != "substring":
                for m in prefix_re.finditer(line):
                    _record_decl(m.group(0), i, p, current, True, decl_name)
            if match != "prefix":
                # 子串命中：片段扩展为完整符号名；已建档的加热度
                for m in substring_frag_re.finditer(line):
                    name = _expand_ident(line, m.start(), m.end())
                    if not name or name[0].isdigit() or prefix not in name:
                        continue
                    if name in cands:
                        cands[name]["refs"] += 1
                    else:
                        _record_decl(name, i, p, current, False, decl_name)

    # 当前文件优先；其余文件在 walk 中扫
    if file_path and os.path.isfile(file_path):
        scan_file_full(file_path, True)
    exts = (".rs", ".py", ".ts", ".js")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.endswith(exts) or os.path.abspath(fn) == os.path.abspath(file_path):
                continue
            scan_file_full(os.path.join(dirpath, fn), False)
    if not cands:
        return {"ok": True, "prefix": prefix, "items": [], "count": 0,
                "match_mode": match, "note": "无匹配"}
    # 排序：当前文件 → 声明 → 前缀命中 → 热度（引用次数降序）→ 名字字典序
    ranked = sorted(cands.values(),
                    key=lambda c: (0 if c["current"] else 1,
                                   0 if c["kind"] == "decl" else 1,
                                   0 if c["prefix_match"] else 1,
                                   -c["refs"],
                                   c["name"]))
    ranked = ranked[:max(limit, 1)]
    used_mode = ("prefix" if all(c["prefix_match"] for c in ranked)
                 else "substring" if match == "substring" else "auto")
    return {"ok": True, "prefix": prefix,
            "items": [c["name"] for c in ranked],
            "detailed": ranked,
            "count": len(ranked),
            "match_mode": used_mode}


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
        # IDE 增强 99：文件级汇总（影响面文件数——AI 评估改动范围）
        "file_count": len({r["file"] for r in refs}),
        "files": sorted({r["file"] for r in refs}),
        "advice": "用 ide_rename 生成重命名方案（L3 建议层，确认后 fs_write 应用）",
    }


# ── ide_actions ────────────────────────────────────────────
# 追加规则（IDE 强度增强 2026-08-13）：TODO/FIXME 未完成标记 + 空 except 吞错
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_EXCEPT_PASS_RE = re.compile(r"except[^:]*:\s*(?:pass\s*)?$|except[^:]*:\s*$")
_NEXT_LINE_PASS_RE = re.compile(r"^\s*pass\s*(#.*)?$")


def _actions_for_file(file_path: str) -> list[dict]:
    """单文件快速修复建议（规则扫描，无 LSP 的降级 code_action）。"""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
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
        # IDE 增强 98：裸 panic!/todo!/unimplemented!（生产代码裸崩溃——
        # 比 unwrap 更直接；测试文件豁免）
        elif re.search(r"\b(panic!|todo!|unimplemented!)\s*\(", s) \
                and "test" not in file_path.lower():
            actions.append({
                "line": i, "title": "裸 panic!/todo!/unimplemented!",
                "detail": f"`{s[:60]}` 生产代码裸崩溃/占位——建议返回 Result 或显式错误处理",
                "kind": "safety",
            })
        elif is_python and ("except" in s and _EXCEPT_PASS_RE.match(s)
                            or _NEXT_LINE_PASS_RE.match(s)):
            actions.append({
                "line": i, "title": "空 except 吞错",
                "detail": f"`{s[:60]}` 静默吞异常——建议记录或显式处理",
                "kind": "safety",
            })
        # IDE 增强 101/255：调试残留（print/dbg!/println!/eprintln!/printf/
        # std::cout 生产代码裸用——建议移除或转日志；测试文件豁免）
        elif re.search(r"\b(print|dbg!|println!|eprintln!|printf)\s*\("
                       r"|std::cout\s*<<", s) \
                and "test" not in file_path.lower():
            actions.append({
                "line": i, "title": "调试残留（print/dbg!/println!/printf）",
                "detail": f"`{s[:60]}` 生产代码调试输出——建议移除或转正式日志",
                "kind": "cleanup",
            })
        # IDE 增强 104：Python == None/!= None（应为 is None/is not None——
        # 语义正确性：== 触发 __eq__ 可能误判）
        elif is_python and re.search(r"(==|!=)\s*None\b", s):
            actions.append({
                "line": i, "title": "== None 应为 is None",
                "detail": f"`{s[:60]}` 建议 is None/is not None（== 可能触发 __eq__ 误判）",
                "kind": "correctness",
            })
        if len(actions) >= 20:
            break
    # IDE 增强三十五：相邻同规则建议合并（连续行同 title → 区间 + count）——
    # 报告更紧凑（连续 3 个 unwrap 不再刷 3 条）
    merged: list[dict] = []
    for a in actions:
        if merged and merged[-1]["title"] == a["title"] \
                and a["line"] == merged[-1].get("line_end", merged[-1]["line"]) + 1:
            merged[-1]["line_end"] = a["line"]
            merged[-1]["count"] = merged[-1].get("count", 1) + 1
        else:
            merged.append(dict(a))
    return merged


# 目录批量上限（IDE 增强四 2026-08-13：防 DoS）
_ACTIONS_MAX_FILES = 50       # 最多扫 50 个文件
_ACTIONS_MAX_TOTAL = 200      # 总建议上限
_ACTIONS_EXTS = (".rs", ".py", ".ts", ".js")


def ide_actions(path: str) -> dict:
    """快速修复建议：文件或目录批量（无 LSP 的降级 code_action）。

    规则：unwrap/expect panic 风险、as 窄化、TODO/FIXME 未完成标记、
    空 except 吞错——kind 区分 safety/cleanup。
    目录模式：递归扫代码文件（≤50 文件 / ≤200 建议，防 DoS），按文件分组。
    """
    if os.path.isdir(path):
        per_file: dict[str, list[dict]] = {}
        total = 0
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames
                           if d not in ("target", "node_modules", ".git", "release")]
            for fn in sorted(filenames):
                if not fn.endswith(_ACTIONS_EXTS):
                    continue
                p = os.path.join(dirpath, fn)
                acts = _actions_for_file(p)
                if acts:
                    per_file[p] = acts
                    total += len(acts)
                scanned += 1
                if scanned >= _ACTIONS_MAX_FILES or total >= _ACTIONS_MAX_TOTAL:
                    return {"ok": True, "path": path,
                            "files_scanned": scanned,
                            "actions_by_file": per_file,
                            "total": total,
                            "truncated": scanned >= _ACTIONS_MAX_FILES or total >= _ACTIONS_MAX_TOTAL,
                            "note": "目录批量模式：每文件≤20 建议，文件≤50，总≤200"}
        return {"ok": True, "path": path, "files_scanned": scanned,
                "actions_by_file": per_file, "total": total,
                "truncated": False,
                "note": "目录批量模式：每文件≤20 建议，文件≤50，总≤200"}
    # 单文件（向后兼容：file/actions/count 字段不变）
    actions = _actions_for_file(path)
    # IDE 增强 112：kind 分布（safety/cleanup/correctness 计数——修复
    # 优先级一眼可见）
    _kinds: dict[str, int] = {}
    for a in actions:
        _k = str(a.get("kind", "")) or "other"
        _kinds[_k] = _kinds.get(_k, 0) + 1
    return {"ok": True, "file": path, "actions": actions,
            "count": len(actions), "kind_counts": _kinds}


# ══════════════ ide_ui（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx 桌面 IDE（tkinter 零依赖）——IDE 界面 + 各种功能窗口。

主窗口（IDE 布局）：
  菜单栏 / 左：项目文件树 / 中：编辑器（行号+语法高亮+保存）/
  右：功能面板（工具调用/扫描/遥测/热榜/日志）/ 底：状态栏

功能窗口（菜单或面板触发）：
  工具调用器（server._call 全链路）/ 扫描面板 / 遥测 / 仪表盘（Canvas
  条形图）/ 扫描日志 / 关于

数据源：~/.unified-rx/（stats/scan-log/telemetry）+ server 注册表——
复用 dashboard.py 的纯读取函数（零重复）。

用法：python ide_ui.py
"""
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = _ENGINE_ROOT
sys.path.insert(0, HERE)
from dashboard import _read_jsonl, _read_stats, _scanlog, _telemetry, _tools  # noqa: E402

DATA_DIR = os.path.expanduser("~/.unified-rx")
START_TS = time.time()

# ── 配色（深色 IDE 风格）──
C = dict(bg="#0d1117", panel="#161b22", line="#30363d", fg="#e6edf3",
         dim="#8b949e", acc="#58a6ff", ok="#3fb950", warn="#d29922",
         err="#f85149", sel="#1f6feb", editor="#0d1117", gutter="#161b22",
         keyword="#ff7b72", string="#a5d6ff", comment="#8b949e",
         number="#79c0ff", fn="#d2a8ff")

# 编辑器语法高亮（关键词集——按语言）
_KW = {
    "python": r"\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|lambda|pass|break|continue|raise|yield|global|nonlocal|and|or|not|in|is|None|True|False|self|async|await)\b",
    "rust": r"\b(fn|let|mut|pub|struct|enum|impl|trait|match|if|else|for|while|loop|return|use|mod|self|Self|async|await|move|ref|dyn|where|const|static|unsafe|type|true|false|Some|None|Ok|Err|Result|Option)\b",
    "json": r"\b(true|false|null)\b",
}
_STR_RE = r"\x22(?:[^\x5c\x22]|\x5c.)*\x22|\x27(?:[^\x5c\x27]|\x5c.)*\x27"
_CMT_RE = {"python": r"#[^\n]*", "rust": r"(//[^\n]*|/\*.*?\*/)"}
_NUM_RE = r"\b\d[\d_.]*\b"


def _lang_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py",):
        return "python"
    if ext in (".rs",):
        return "rust"
    if ext in (".json",):
        return "json"
    return ""


def _fmt_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=1)
    except Exception:
        return str(obj)

# ─────────────────────────────────────────────────────────────
# 编辑器组件：行号 + Text + 语法高亮 + 保存
# ─────────────────────────────────────────────────────────────
class CodeEditor(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.path = None
        self.lang = ""
        self._gutter = tk.Canvas(self, width=46, bg=C["gutter"],
                                 highlightthickness=0)
        self._gutter.pack(side="left", fill="y")
        self._text = tk.Text(self, bg=C["editor"], fg=C["fg"],
                             insertbackground=C["fg"], wrap="none",
                             font=("Consolas", 11), undo=True,
                             relief="flat", padx=6, pady=4)
        self._text.pack(side="left", fill="both", expand=True)
        self._sb = ttk.Scrollbar(self, command=self._text.yview)
        self._sb.pack(side="right", fill="y")
        self._text.configure(yscrollcommand=self._sync_scroll)
        self._text.bind("<KeyRelease>", lambda e: self._highlight())
        self._text.bind("<Control-s>", lambda e: self.save())
        self._text.bind("<Control-o>", lambda e: self.open_dialog())
        self._text.bind("<Configure>", lambda e: self._draw_gutter())

    def _sync_scroll(self, *a):
        self._sb.set(*a)
        self._draw_gutter()

    def _draw_gutter(self):
        self._gutter.delete("all")
        first = self._text.index("@0,0")
        last = self._text.index("@0,1000000")
        line = int(first.split(".")[0])
        end = int(last.split(".")[0])
        y = 0
        for ln in range(line, end + 1):
            idx = self._text.index(f"{ln}.0")
            y = self._text.dlineinfo(idx)
            if y is None:
                continue
            self._gutter.create_text(40, y[1] + 2, anchor="ne",
                                     text=str(ln), fill=C["dim"],
                                     font=("Consolas", 10))

    def open_file(self, path):
        try:
            size = os.path.getsize(path)
            if size > 1_000_000:
                messagebox.showwarning("文件过大", f"{os.path.basename(path)} 超过 1MB，已跳过")
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror("打开失败", str(e))
            return
        self.path = path
        self.lang = _lang_of(path)
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)
        self._text.edit_reset()
        self._highlight()
        self._draw_gutter()

    def open_dialog(self):
        p = filedialog.askopenfilename(
            title="打开文件", initialdir=os.path.expanduser("~"))
        if p:
            self.open_file(p)

    def save(self):
        if not self.path:
            p = filedialog.asksaveasfilename(title="保存为", defaultextension=".txt")
            if not p:
                return
            self.path = p
        try:
            with open(self.path, "w", encoding="utf-8", newline="") as f:
                f.write(self._text.get("1.0", "end-1c"))
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.master.status("已保存 " + os.path.basename(self.path))

    def _clear_tags(self):
        for t in ("kw", "str", "cmt", "num", "fnc"):
            self._text.tag_remove(t, "1.0", "end")

    def _highlight(self):
        if not self.lang:
            return
        self._clear_tags()
        text = self._text.get("1.0", "end-1c")
        n = len(text)
        if n > 500_000:  # 大文件跳过高亮（防卡）
            return
        base = "1.0"

        def tag(pat, tag_name, group=0, flags=0):
            try:
                for m in re.finditer(pat, text, flags):
                    s = self._text.index(f"{base}+{m.start(group)}c")
                    e = self._text.index(f"{base}+{m.end(group)}c")
                    self._text.tag_add(tag_name, s, e)
            except (re.error, tk.TclError):
                pass

        # 先字符串/注释（高优先级底色），再关键词
        tag(_STR_RE, "str", flags=re.S)
        cmt = _CMT_RE.get(self.lang)
        if cmt:
            tag(cmt, "cmt", flags=re.S)
        tag(_KW.get(self.lang, ""), "kw", flags=re.I)
        tag(_NUM_RE, "num")
        # 函数调用 fnc( 高亮（python/rust）
        tag(r"([a-zA-Z_]\w*)(?=\s*\()", "fnc")
        for t, color in (("kw", C["keyword"]), ("str", C["string"]),
                         ("cmt", C["comment"]), ("num", C["number"]),
                         ("fnc", C["fn"])):
            self._text.tag_configure(t, foreground=color)

# ─────────────────────────────────────────────────────────────
# 文件树（懒加载：展开时读子目录）
# ─────────────────────────────────────────────────────────────
_SKIP_DIRS = {".git", "target", "__pycache__", "node_modules", ".idea",
              ".vscode", "vendor", "dist", "build", ".unified-rx-index"}


class FileTree(tk.Frame):
    def __init__(self, master, on_open, **kw):
        super().__init__(master, **kw)
        self.on_open = on_open
        self.root_path = None
        self._tree = ttk.Treeview(self, show="tree", selectmode="browse")
        self._tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self, command=self._tree.yview)
        sb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<<TreeviewOpen>>", self._on_open_node)
        self._tree.bind("<Double-1>", self._on_double)
        self._tree.bind("<Return>", self._on_double)

    def load_root(self, path):
        self.root_path = path
        self._tree.delete(*self._tree.get_children())
        node = self._tree.insert("", "end", text=os.path.basename(path) or path,
                                 open=True)
        self._load_children(node, path)

    def _load_children(self, node, path):
        try:
            entries = sorted(os.scandir(path), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in _SKIP_DIRS:
                continue
            if e.is_dir():
                child = self._tree.insert(node, "end", text="📁 " + e.name,
                                          open=False)
                # 占位子节点（触发懒加载）
                self._tree.insert(child, "end", text="")
            else:
                self._tree.insert(node, "end", text=e.name,
                                  tags=("file",),
                                  values=(os.path.join(path, e.name),))

    def _on_open_node(self, event):
        node = event.widget.focus()
        kids = self._tree.get_children(node)
        if len(kids) == 1 and self._tree.item(kids[0], "text") == "":
            self._tree.delete(kids[0])
            path = self._node_path(node)
            if path and os.path.isdir(path):
                self._load_children(node, path)

    def _node_path(self, node):
        parts = []
        while node:
            parts.append(self._tree.item(node, "text").replace("📁 ", ""))
            node = self._tree.parent(node)
        if not parts:
            return None
        parts.reverse()
        head = next(iter(parts), "")
        if self.root_path and os.path.basename(self.root_path) == head:
            return os.path.join(self.root_path, *parts[1:])
        return os.path.join(*parts) if len(parts) > 1 else head

    def _on_double(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        if vals:
            self.on_open(vals[0])

# ─────────────────────────────────────────────────────────────
# 功能面板（右栏 Notebook）：工具调用 / 扫描 / 热榜 / 日志
# ─────────────────────────────────────────────────────────────
class ToolPanel(tk.Frame):
    """工具调用器：下拉选工具 → JSON 参数 → 运行 → 结果。"""

    def __init__(self, master, server, **kw):
        super().__init__(master, **kw)
        self.server = server
        self._tools = sorted(server._TOOLS.keys())
        row = tk.Frame(self, bg=C["panel"])
        row.pack(fill="x", padx=6, pady=4)
        tk.Label(row, text="工具", bg=C["panel"], fg=C["dim"]).pack(side="left")
        self._combo = ttk.Combobox(row, values=self._tools, width=28)
        self._combo.pack(side="left", padx=6)
        self._combo.bind("<<ComboboxSelected>>", lambda e: self._load_args())
        self._run_btn = tk.Button(row, text="▶ 运行", command=self.run,
                                  bg=C["sel"], fg="white", relief="flat")
        self._run_btn.pack(side="left", padx=6)
        tk.Label(self, text="参数（JSON，可空）", bg=C["panel"],
                 fg=C["dim"]).pack(anchor="w", padx=6)
        self._args = tk.Text(self, height=5, bg=C["editor"], fg=C["fg"],
                             insertbackground=C["fg"], font=("Consolas", 10),
                             relief="flat")
        self._args.pack(fill="x", padx=6)
        tk.Label(self, text="结果", bg=C["panel"], fg=C["dim"]).pack(anchor="w", padx=6)
        self._out = tk.Text(self, bg=C["editor"], fg=C["fg"], wrap="none",
                            font=("Consolas", 10), relief="flat")
        self._out.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _load_args(self):
        name = self._combo.get()
        fn, sc, _ = self.server._TOOLS.get(name, (None, None, None))
        if sc:
            self._args.delete("1.0", "end")
            props = sc.get("properties", {})
            req = sc.get("required", [])
            self._args.insert("1.0", _fmt_json({r: "" for r in req}))

    def run(self):
        name = self._combo.get()
        if not name:
            return
        raw = self._args.get("1.0", "end-1c").strip()
        args = {}
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                self._out.delete("1.0", "end")
                self._out.insert("1.0", f"参数 JSON 非法: {e}")
                return
        self._out.delete("1.0", "end")
        self._out.insert("1.0", "运行中…")

        def work():
            try:
                r = self.server._call(name, args)
                text = r[0].text if isinstance(r, list) else str(r)
                out = _fmt_json(json.loads(text)) if text.startswith("{") else text
            except Exception as e:  # noqa: BLE001
                out = f"Error: {type(e).__name__}: {e}"
            self.after(0, lambda: self._set_out(out))

        threading.Thread(target=work, daemon=True).start()

    def _set_out(self, text):
        self._out.delete("1.0", "end")
        self._out.insert("1.0", text)


class ScanPanel(tk.Frame):
    """扫描：bug_scan/std_check/vuln_scan/ui_check → 结果表。"""

    def __init__(self, master, server, **kw):
        super().__init__(master, **kw)
        self.server = server
        row = tk.Frame(self, bg=C["panel"])
        row.pack(fill="x", padx=6, pady=4)
        tk.Label(row, text="路径", bg=C["panel"], fg=C["dim"]).pack(side="left")
        self._path = tk.Entry(row, bg=C["editor"], fg=C["fg"],
                              insertbackground=C["fg"])
        self._path.pack(side="left", fill="x", expand=True, padx=6)
        self._path.insert(0, HERE)
        self._kind = ttk.Combobox(row, width=12, values=(
            "bug_scan", "std_check", "vuln_scan", "ui_check"))
        self._kind.set("bug_scan")
        self._kind.pack(side="left")
        tk.Button(row, text="▶ 扫描", command=self.run,
                  bg=C["sel"], fg="white", relief="flat").pack(side="left", padx=6)
        cols = ("severity", "file", "line", "msg")
        self._tree = ttk.Treeview(self, columns=cols, show="headings",
                                  height=12)
        for c, w in zip(cols, (70, 180, 50, 420)):
            self._tree.heading(c, text=c)
            self._tree.column(c, width=w)
        self._tree.pack(fill="both", expand=True, padx=6, pady=4)

    def run(self):
        path = self._path.get().strip()
        kind = self._kind.get()
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._tree.insert("", "end", values=("…", kind, "", "扫描中"))

        def work():
            try:
                r = self.server._call(kind, {"path": path})
                text = r[0].text if isinstance(r, list) else str(r)
                d = json.loads(text) if text.startswith("{") else {}
                issues = d.get("issues", []) if isinstance(d, dict) else []
                rows = [(i.get("severity", "?"), (i.get("file") or "").split("\\")[-1],
                         i.get("line", ""), (i.get("msg") or "")[:120])
                        for i in issues]
            except Exception as e:  # noqa: BLE001
                rows = [("ERR", kind, "", str(e)[:120])]
            self.after(0, lambda: self._set_rows(rows))

        threading.Thread(target=work, daemon=True).start()

    def _set_rows(self, rows):
        for i in self._tree.get_children():
            self._tree.delete(i)
        for r in rows:
            tag = ""
            if r[0] in ("error", "ERR"):
                tag = "err"
            elif r[0] == "warning":
                tag = "warn"
            self._tree.insert("", "end", values=r, tags=(tag,))
        self._tree.tag_configure("err", foreground=C["err"])
        self._tree.tag_configure("warn", foreground=C["warn"])


class StatsPanel(tk.Frame):
    """热榜（Canvas 条形图）+ 累计统计。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._canvas = tk.Canvas(self, bg=C["panel"], highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self.after(2000, self.refresh)

    def refresh(self):
        st = _read_stats()
        top = sorted(st.get("by_tool", {}).items(),
                     key=lambda kv: -kv[1])[:10]
        self._canvas.delete("all")
        cw = self._canvas.winfo_width() or 360
        ch = self._canvas.winfo_height() or 300
        maxv = max((v for _, v in top), default=1)
        self._canvas.create_text(cw // 2, 14, text=f"TOP10 · 累计 {st.get('total', 0):,} 次",
                                 fill=C["fg"], font=("Segoe UI", 11, "bold"))
        y = 34
        for name, v in top:
            w = max(10, int((cw - 180) * v / maxv))
            self._canvas.create_text(140, y + 8, text=name, anchor="e",
                                     fill=C["dim"], font=("Consolas", 9))
            self._canvas.create_rectangle(150, y, 150 + w, y + 16,
                                          fill=C["acc"], outline="")
            self._canvas.create_text(160 + w, y + 8, text=f"{v:,}", anchor="w",
                                     fill=C["fg"], font=("Consolas", 9))
            y += 22
        self.after(3000, self.refresh)


class LogPanel(tk.Frame):
    """scan-log 最近记录。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        cols = ("ts", "tool", "ok", "summary")
        self._tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, w in zip(cols, (110, 90, 40, 460)):
            self._tree.heading(c, text=c)
            self._tree.column(c, width=w)
        self._tree.pack(fill="both", expand=True)
        self.refresh()
        self.after(5000, self._auto)

    def _auto(self):
        self.refresh()
        self.after(5000, self._auto)

    def refresh(self):
        recs = _scanlog(30)
        for i in self._tree.get_children():
            self._tree.delete(i)
        for r in recs:
            self._tree.insert("", "end", values=(
                str(r.get("ts", ""))[:19], r.get("tool", ""),
                "OK" if r.get("ok") else "FAIL", r.get("summary", "")))

# ─────────────────────────────────────────────────────────────
# 独立功能窗口（Toplevel）：遥测 / 关于
# ─────────────────────────────────────────────────────────────
class TelemetryWin(tk.Toplevel):
    """遥测窗口：慢工具 TOP / 错误率 / daemon 心跳。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("遥测")
        self.geometry("520x420")
        self.configure(bg=C["bg"])
        self._txt = tk.Text(self, bg=C["editor"], fg=C["fg"], wrap="word",
                            font=("Consolas", 10), relief="flat")
        self._txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.refresh()
        self.after(3000, self._auto)

    def _auto(self):
        if self.winfo_exists():
            self.refresh()
            self.after(3000, self._auto)

    def refresh(self):
        tel = _telemetry(300)
        ov = None
        try:
            import dashboard
            ov = dashboard._overview()
        except Exception:
            pass
        lines = [f"遥测样本（最近 300）: {tel['samples']}",
                 f"错误率: {tel['err_rate'] * 100:.1f}%（{tel['err_count']} 次）", ""]
        lines.append("最慢工具 TOP8:")
        for s in tel["slowest"]:
            lines.append(f"  {s['tool']:<24} {s['ms']:>9.1f}ms  {s['status']}")
        if ov and ov.get("heartbeats"):
            lines.append("")
            lines.append("daemon 心跳:")
            now = time.time()
            for k, ts in ov["heartbeats"].items():
                age = now - ts
                lines.append(f"  {k:<20} {age:.0f}s 前" + (" ✓" if age < 300 else " ✗"))
        self._txt.delete("1.0", "end")
        self._txt.insert("1.0", "\n".join(lines))


class AboutWin(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("关于")
        self.geometry("380x300")
        self.configure(bg=C["bg"])
        t = _tools()
        st = _read_stats()
        info = (f"unified-rx 桌面 IDE\n\n"
                f"工具: {t.get('total', 0)}（核心 {t.get('core_count', 0)} + 扩展 {t.get('ext_count', 0)}）\n"
                f"累计调用: {st.get('total', 0):,}\n\n"
                f"数据目录: {DATA_DIR}\n"
                f"仓库: {HERE}\n\n"
                f"tkinter {tk.TkVersion} · 零第三方依赖\n"
                f"2026-08-16 · bfxh")
        tk.Label(self, text=info, bg=C["bg"], fg=C["fg"], justify="left",
                 font=("Segoe UI", 11)).pack(padx=20, pady=20, anchor="w")

# ─────────────────────────────────────────────────────────────
# 主窗口：菜单 + 文件树 + 编辑器 + 功能面板 + 状态栏
# ─────────────────────────────────────────────────────────────
class IdeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("unified-rx 桌面 IDE")
        self.geometry("1280x800")
        self.configure(bg=C["bg"])
        try:
            import server as S
            self.server = S
        except Exception as e:  # noqa: BLE001
            self.server = None
            messagebox.showwarning("server 不可用", str(e))
        self._build_layout()
        self._build_menu()
        self.status("就绪")
        self.after(3000, self._tick)

    # ── 布局 ──
    def _build_layout(self):
        main = tk.PanedWindow(self, orient="horizontal", bg=C["bg"],
                              sashwidth=4, sashrelief="flat")
        main.pack(fill="both", expand=True)
        # 左：文件树
        left = tk.Frame(main, bg=C["panel"])
        tk.Label(left, text="📁 项目文件", bg=C["panel"], fg=C["dim"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=4)
        self._tree = FileTree(left, on_open=self._open_file)
        self._tree.pack(fill="both", expand=True, padx=4, pady=4)
        tk.Button(left, text="… 选择项目目录", command=self._choose_root,
                  bg=C["panel"], fg=C["acc"], relief="flat",
                  activebackground=C["line"]).pack(fill="x", padx=4, pady=(0, 4))
        main.add(left, width=240, minsize=160)
        # 中：编辑器
        mid = tk.Frame(main, bg=C["panel"])
        self._editor = CodeEditor(mid)
        self._editor.pack(fill="both", expand=True, padx=2, pady=2)
        main.add(mid, width=640, minsize=320)
        # 右：功能面板
        right = tk.Frame(main, bg=C["panel"])
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        if self.server:
            nb.add(ToolPanel(nb, self.server), text="🔧 工具")
            nb.add(ScanPanel(nb, self.server), text="🩺 扫描")
        nb.add(StatsPanel(nb), text="📊 热榜")
        nb.add(LogPanel(nb), text="📜 日志")
        main.add(right, width=400, minsize=280)
        # 底：状态栏
        bar = tk.Frame(self, bg=C["panel"], height=26)
        bar.pack(fill="x", side="bottom")
        self._status_lbl = tk.Label(bar, text="", bg=C["panel"], fg=C["dim"],
                                    anchor="w", font=("Segoe UI", 9))
        self._status_lbl.pack(side="left", padx=8)
        self._tick_lbl = tk.Label(bar, text="", bg=C["panel"], fg=C["ok"],
                                  font=("Consolas", 9))
        self._tick_lbl.pack(side="right", padx=8)

    # ── 菜单 ──
    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="打开文件…  Ctrl+O", command=self._editor.open_dialog)
        fm.add_command(label="保存  Ctrl+S", command=self._editor.save)
        fm.add_separator()
        fm.add_command(label="退出", command=self.destroy)
        m.add_cascade(label="文件", menu=fm)
        tm = tk.Menu(m, tearoff=0)
        tm.add_command(label="工具调用器", command=lambda: self._panel_tab(0))
        tm.add_command(label="扫描", command=lambda: self._panel_tab(1))
        tm.add_command(label="遥测窗口", command=self._open_telemetry)
        tm.add_command(label="仪表盘网页 (:17300)", command=self._open_web)
        m.add_cascade(label="工具", menu=tm)
        vm = tk.Menu(m, tearoff=0)
        vm.add_command(label="切换左右面板", command=self._toggle_tree)
        m.add_cascade(label="视图", menu=vm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="关于", command=self._open_about)
        m.add_cascade(label="帮助", menu=hm)
        self.config(menu=m)

    # ── 交互 ──
    def _choose_root(self):
        p = filedialog.askdirectory(initialdir=os.path.expanduser("~"))
        if p:
            self._tree.load_root(p)
            self.status("项目: " + p)

    def _open_file(self, path):
        if os.path.isfile(path):
            self._editor.open_file(path)
            self.status(os.path.basename(path))

    def _panel_tab(self, idx):
        nb = self.winfo_children()[0].winfo_children()[1].winfo_children()[0]
        try:
            nb.select(idx)
        except Exception:
            pass

    def _open_telemetry(self):
        TelemetryWin(self)

    def _open_about(self):
        AboutWin(self)

    def _open_web(self):
        import webbrowser
        webbrowser.open("http://127.0.0.1:17300")

    def _toggle_tree(self):
        pass  # 面板宽度由 PanedWindow sash 拖动，保留菜单项

    def status(self, msg):
        self._status_lbl.configure(text="  " + msg)

    # ── 状态栏定时刷新（3s）──
    def _tick(self):
        t = _tools()
        st = _read_stats()
        ov = None
        try:
            import dashboard
            ov = dashboard._overview()
        except Exception:
            pass
        fresh = ov["data_latest_age_s"] if ov else -1
        color = C["ok"] if 0 <= fresh < 600 else C["warn"]
        self._tick_lbl.configure(
            text=f"工具 {t.get('total', 0)} · 调用 {st.get('total', 0):,} · 数据 {fresh}s 前",
            fg=color)
        self.after(3000, self._tick)


def main() -> int:
    app = IdeApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ══════════════ ide_session（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_session.py — 编辑会话模型（IDE_ENHANCE_PLAN R5，抄 AetherStudio）。

  FastLineIndex — 行号 ↔ 字节偏移 O(log n) 转换（升 lsp_position_convert；
                  大文件 5000+ 行编辑场景每次定位不用全文件扫行）
  PieceTable   — 增量编辑文档模型：original + add buffers + 编辑列表，
                  只算增量 diff 不重写全文；piece 合并防碎片

两者纯 Python 无依赖（可独立测试）。
"""

import bisect
import itertools
from dataclasses import dataclass, field

# 历史栈上限（undo/redo 最多保留的编辑快照数——防内存膨胀）
_HISTORY_LIMIT = 100


# ── FastLineIndex ──────────────────────────────────────────
class FastLineIndex:
    """行起始偏移索引：offsets[i] = 第 i 行（0-based）起始字节偏移。

    构建 O(n)，行号/偏移互转 O(log n)。大文件（5000+ 行）优于逐行扫描。
    """

    def __init__(self, text: str):
        self.text = text
        self._offsets = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._offsets.append(i + 1)

    @property
    def line_count(self) -> int:
        return len(self._offsets)

    def line_start(self, line: int) -> int:
        """行起始偏移（越界返回文本末尾）。"""
        if line < 0:
            return 0
        if line >= self.line_count:
            return len(self.text)
        return self._offsets[line]

    def line_end(self, line: int) -> int:
        """行结束偏移（不含换行符）。"""
        if line < 0 or line >= self.line_count:
            return len(self.text)
        nxt = self._offsets[line + 1] if line + 1 < self.line_count else len(self.text)
        end = nxt - 1
        if end < 0:
            return 0  # 空文本：行 0 无内容
        return end if self.text[end:end + 1] != "\n" else nxt

    def position_to_offset(self, line: int, col: int) -> int:
        """(行,列) → 字节偏移。col 按字符（code point）计。"""
        start = self.line_start(line)
        return start + min(col, self.line_end(line) - start)

    def offset_to_position(self, offset: int) -> tuple[int, int]:
        """字节偏移 → (行, 列)。二分查找行。"""
        offset = max(0, min(offset, len(self.text)))
        i = bisect.bisect_right(self._offsets, offset) - 1
        return i, offset - self._offsets[i]


# ── PieceTable ─────────────────────────────────────────────
@dataclass
class _Piece:
    """一段文本：source（0=原始, 1+=追加缓冲）+ 起止偏移。"""
    source: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class PieceTable:
    """增量编辑文档模型（AetherStudio 同款思想，纯 Python 简化版）。

    insert/delete 只追加编辑记录（O(1) 摊销），text() 时按 piece 重建。
    编辑后行索引变化通过 FastLineIndex(text()) 即时重建。

    IDE 强度增强（2026-08-13）：undo/redo 历史栈（_HISTORY_LIMIT 上限）——
    pieces 列表浅拷贝快照（_Piece 不可变、buffers append-only，恢复安全）；
    每次编辑前压快照，undo 回退、redo 重做。
    """

    original: str = ""
    _buffers: list[str] = field(default_factory=list)
    _pieces: list[_Piece] = field(default_factory=list)
    _history: list[list[_Piece]] = field(default_factory=list)
    _redo_stack: list[list[_Piece]] = field(default_factory=list)

    def __post_init__(self):
        if self.original:
            self._pieces = [_Piece(0, 0, len(self.original))]

    # ── undo/redo ──
    def _snapshot(self) -> None:
        """编辑前压入历史快照（浅拷贝 pieces——_Piece 不可变，安全）。"""
        self._history.append(self._pieces[:])
        if len(self._history) > _HISTORY_LIMIT:
            self._history.pop(0)
        self._redo_stack.clear()

    def undo(self) -> bool:
        """回退一次编辑。返回是否有可回退的历史。"""
        if not self._history:
            return False
        self._redo_stack.append(self._pieces[:])
        self._pieces = self._history.pop()
        return True

    def redo(self) -> bool:
        """重做一次被撤销的编辑。返回是否有可重做的历史。"""
        if not self._redo_stack:
            return False
        self._history.append(self._pieces[:])
        self._pieces = self._redo_stack.pop()
        return True

    def can_undo(self) -> bool:
        return bool(self._history)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def history_depth(self) -> int:
        return len(self._history)

    # ── 编辑操作 ──
    def insert(self, offset: int, text: str) -> None:
        if not text:
            return
        self._snapshot()
        self._buffers.append(text)
        src = len(self._buffers)  # buffer 索引（1-based；0=original）
        self._insert_piece(offset, _Piece(src, 0, len(text)))

    def delete(self, start: int, end: int) -> None:
        if end <= start:
            return
        self._snapshot()
        self._delete_range(start, end)

    def replace(self, start: int, end: int, text: str) -> None:
        if end <= start and not text:
            return
        self._snapshot()
        self._delete_range(start, end)
        if text:
            self._buffers.append(text)
            src = len(self._buffers)
            self._insert_piece(start, _Piece(src, 0, len(text)))

    # ── 内部：piece 切分/插入/删除 ──
    def _split_at(self, offset: int) -> int:
        """在 offset 处切分 piece 边界，返回 piece 索引（offset 恰在边界则返回该索引）。"""
        cur = 0
        for i, p in enumerate(self._pieces):
            if cur <= offset <= cur + p.length:
                if offset > cur and offset < cur + p.length:
                    # 切分
                    cut = offset - cur
                    left = _Piece(p.source, p.start, p.start + cut)
                    right = _Piece(p.source, p.start + cut, p.end)
                    self._pieces[i:i + 1] = [left, right]
                    return i + 1
                return i if offset == cur else i + 1
            cur += p.length
        return len(self._pieces)

    def _insert_piece(self, offset: int, piece: _Piece) -> None:
        idx = self._split_at(offset)
        self._pieces.insert(idx, piece)
        self._merge_adjacent(idx)

    def _delete_range(self, start: int, end: int) -> None:
        left = self._split_at(start)
        right = self._split_at(end)
        del self._pieces[left:right]

    def _merge_adjacent(self, idx: int) -> None:
        """合并相邻同源 piece（防碎片）。"""
        i = max(0, idx - 1)
        while i + 1 < len(self._pieces):
            a, b = self._pieces[i], self._pieces[i + 1]
            if a.source == b.source and a.end == b.start:
                self._pieces[i] = _Piece(a.source, a.start, b.end)
                del self._pieces[i + 1]
            else:
                i += 1

    # ── 读取 ──
    def _piece_text(self, p: _Piece) -> str:
        buf = self.original if p.source == 0 else self._buffers[p.source - 1]
        return buf[p.start:p.end]

    def text(self) -> str:
        return "".join(self._piece_text(p) for p in self._pieces)

    def length(self) -> int:
        return sum(p.length for p in self._pieces)

    def line_index(self) -> FastLineIndex:
        return FastLineIndex(self.text())

    def edit_count(self) -> int:
        return len(self._buffers)  # 追加缓冲数 ≈ 编辑批次数


# ══════════════ ide_commands（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_commands.py — 内建命令手册 + 本地执行（MCP_OPTIMIZATION_PLAN M3）。

智能体做项目时不用反复试错/搜索"该用什么命令"——常用命令写死：
  cmd_cheatsheet — 按域查命令手册（cargo/blender/git/python）
  local_run     — 执行内建命令模板（参数化，结果结构化返回）

省 token：每次任务省 3-5 轮"怎么编译/怎么测试"的试错。
"""

import subprocess
import shutil

# ── 命令手册（按域）──────────────────────────────────────
_CHEATSHEET: dict[str, list[dict]] = {
    "cargo": [
        {"name": "build", "cmd": "cargo build", "desc": "编译（debug）"},
        {"name": "build_release", "cmd": "cargo build --release -p {pkg}", "desc": "编译 release（产物在 target/release）"},
        {"name": "check", "cmd": "cargo check -p {pkg}", "desc": "快速类型检查（不产出二进制）"},
        {"name": "test", "cmd": "cargo test --workspace", "desc": "全量测试"},
        {"name": "test_one", "cmd": "cargo test -p {pkg} {test_name}", "desc": "单测试"},
        {"name": "clippy", "cmd": "cargo clippy --workspace", "desc": "Lint 检查"},
        {"name": "run", "cmd": "cargo run -p {pkg}", "desc": "运行 debug 版"},
        {"name": "fmt", "cmd": "cargo fmt --check", "desc": "格式检查"},
    ],
    "git": [
        {"name": "status", "cmd": "git status --short", "desc": "工作区状态"},
        {"name": "commit", "cmd": "git add -A; git commit -m \"{msg}\"", "desc": "提交"},
        {"name": "log", "cmd": "git log --oneline -{n}", "desc": "最近提交"},
        {"name": "diff", "cmd": "git diff {path}", "desc": "查看改动"},
    ],
    "python": [
        {"name": "pytest_all", "cmd": "python -X utf8 -m pytest {tests} -q", "desc": "全量测试"},
        {"name": "pytest_one", "cmd": "python -X utf8 -m pytest {file} -q", "desc": "单文件测试"},
        {"name": "script", "cmd": "python -X utf8 {script}", "desc": "跑脚本"},
    ],
    "blender": [
        {"name": "headless_model", "cmd": r'"D:/rj/GJ/Blender 5.2\blender.exe" --background --python {script} -- {args}',
         "desc": "Blender 无头建模（D:/rj/GJ/Blender 5.2）"},
        {"name": "export_glb", "cmd": "Blender 内 io_bevy_export.py（N 面板/Ctrl+Shift+E）",
         "desc": "Bevy 直通导出（assets/models/）"},
    ],
    # IDE 增强 292：多语言命令（各语言测试/检查/运行——AI 一键知道
    # 每个语言的工程命令，配合 languages 画像使用）
    "lang_go": [
        {"name": "test", "cmd": "go test ./...", "desc": "全量测试"},
        {"name": "vet", "cmd": "go vet ./...", "desc": "静态检查"},
        {"name": "fmt", "cmd": "gofmt -l .", "desc": "格式检查"},
    ],
    "lang_ts": [
        {"name": "test", "cmd": "npx vitest run", "desc": "全量测试"},
        {"name": "lint", "cmd": "npx eslint .", "desc": "Lint 检查"},
        {"name": "check", "cmd": "npx tsc --noEmit", "desc": "类型检查"},
    ],
    "lang_cs": [
        {"name": "test", "cmd": "dotnet test", "desc": "全量测试"},
        {"name": "build", "cmd": "dotnet build", "desc": "编译"},
    ],
    "lang_dart": [
        {"name": "test", "cmd": "flutter test", "desc": "Flutter 全量测试"},
        {"name": "analyze", "cmd": "flutter analyze", "desc": "静态检查"},
    ],
    "voxelforge": [
        {"name": "release_deploy", "cmd": "cargo build --release -p nexus_app; Stop-Process -Name nexus_app; Copy-Item target\\release\\nexus_app.exe release\\; Copy-Item assets\\models release\\assets\\models -Recurse",
         "desc": "发布流程：编译→杀进程→复制 exe→同步资产（VoxelForge）"},
        {"name": "test_workspace", "cmd": "cargo test --workspace", "desc": "全量测试（207 目标）"},
        {"name": "run_release", "cmd": "Start-Process release\\nexus_app.exe -WorkingDirectory release", "desc": "运行发布版"},
    ],
    "unifiedrx": [
        {"name": "test", "cmd": "python -X utf8 -m pytest test_unified_rx.py test_enhancements.py test_enhancements2.py test_rustscan.py test_ide_*.py -q",
         "desc": "unified-rx 全量测试（183+）"},
        {"name": "sync_e", "cmd": "Copy-Item *.py test_*.py E:\\共享\\51\\unified-rx\\",
         "desc": "同步运行版 E:"},
        {"name": "bug_hunt", "cmd": "pipeline({preset: bug_hunt, path: ...})", "desc": "默认挖漏洞链"},
        # IDE 增强 111：常驻自扫显式入口（daemon --once：self+project 增量双维度）
        {"name": "self_scan", "cmd": "python -X utf8 daemon.py --once",
         "desc": "常驻自扫一轮（双维度增量，结果落知识库）"},
        # IDE 增强 158：IDE 测试一键（IDE 链回归入口；显式列文件——
        # Windows 下 shell 不展开 test_ide_*.py glob）
        {"name": "pytest_ide",
         "cmd": "python -X utf8 -m pytest test_ide_tools.py test_ide_session.py "
                "test_ide_quest_fusion.py test_ide_baseline.py test_ide_cache.py "
                "test_ide_permission.py test_ide_tiers.py test_unified_rx.py "
                "test_rustscan.py -q",
         "desc": "IDE/扫描链测试一键回归"},
    ],
}


def cheatsheet(domain: str | None = None) -> dict:
    """命令手册查询。domain=None 返回全部。"""
    if domain:
        return {"ok": True, "domain": domain,
                "commands": _CHEATSHEET.get(domain, []),
                "hint": "用 local_run 执行（name + args）",
                # IDE 增强 136：用法示例（调用方一步直达）
                "usage": f'local_run({{"domain": "{domain}", "name": "<上表 name>", '
                         f'"args": {{占位符: 值}}}})'}
    return {"ok": True, "domains": list(_CHEATSHEET.keys()),
            "total": sum(len(v) for v in _CHEATSHEET.values()),
            "hint": "按域查：cmd_cheatsheet({domain: 'cargo'})"}


def local_run(domain: str, name: str, args: dict | None = None,
              workdir: str | None = None, timeout: int = 300) -> dict:
    """执行内建命令模板。args 里的 {key} 占位符由参数填充。

    安全：只允许 _CHEATSHEET 里定义的命令（参数注入占位符——命令名不可控）。
    """
    args = args or {}
    cmds = _CHEATSHEET.get(domain, [])
    entry = next((c for c in cmds if c["name"] == name), None)
    if entry is None:
        return {"ok": False, "error": f"未知命令: {domain}/{name}",
                "available": [c["name"] for c in cmds]}
    template = entry["cmd"]
    # 安全（security-review B602）：shell=True 下参数必须白名单校验——
    # 拒绝 shell 元字符（&|;<>`$(){}[]!^ 与换行），防占位符参数注入命令
    _SAFE_ARG = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "abcdefghijklmnopqrstuvwxyz0123456789 _-./:,")
    for _k, _v in (args or {}).items():
        if not isinstance(_v, str) or not set(_v) <= _SAFE_ARG:
            return {"ok": False,
                    "error": f"参数 {_k} 含不安全字符（仅允许字母/数字/空格/"
                             f"-_.:,/），拒绝执行"}

    try:
        cmd = template.format(**args)
    except KeyError as e:
        return {"ok": False, "error": f"缺少参数: {e}",
                "template": template}
    cwd = workdir or os.getcwd()
    _t0 = time.perf_counter()
    try:
        # 分号连接的多命令（如 release_deploy）在 PowerShell 语义下由 shell 执行
        r = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return {"ok": r.returncode == 0,
                "domain": domain, "name": name,
                "cmd": cmd,
                "exit": r.returncode,
                # IDE 增强 231：命令耗时（ms——性能可见）
                "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
                "stdout_tail": (r.stdout or "")[-1500:],
                "stderr_tail": (r.stderr or "")[-500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"超时（>{timeout}s）", "cmd": cmd}
    except OSError as e:
        return {"ok": False, "error": f"执行失败: {e}", "cmd": cmd}


# ══════════════ ide_cache（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_cache.py — IDE 增量同步缓存（IDE_ENHANCE_PLAN R1）。

文件版本跟踪（mtime_ns + size + 首尾哈希）→ LSP 结果缓存：
- 版本未变 → 直接返回缓存（省 LSP spawn + token，大仓库 IDE 操作 token 省 90%+）
- 版本变了 → 重新查询 + 更新缓存
- LRU 上限防膨胀

设计（抄 AetherStudio delta 增量思想）：缓存键 = path + kind（诊断/符号/hover/references）。
"""

import hashlib
import sqlite3

# 缓存上限（条目数——LRU 淘汰）
_MAX_ENTRIES = 512
# 版本计算采样：头/尾各 N 字节（大文件不全量哈希——版本判断够用）
_SAMPLE_BYTES = 4096

_lock = threading.Lock()
# path -> {version: str, entries: {kind: {"data": ..., "ts": float}}}
_CACHE: dict = {}

# ── 温层持久化（R3：SQLite KV——进程重启恢复，冷查询不重新跑 LSP）──
_WARM_DB: str | None = None  # None = 未启用持久化
_DB_LOCK = threading.Lock()


def enable_persistence(db_path: str) -> None:
    """启用温层持久化（SQLite）。调用一次（幂等）。"""
    global _WARM_DB
    _WARM_DB = db_path
    with _DB_LOCK, sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ide_cache("
            "path TEXT NOT NULL, kind TEXT NOT NULL, "
            "version TEXT NOT NULL, data TEXT NOT NULL, ts REAL NOT NULL, "
            "PRIMARY KEY(path, kind))"
        )
    # 启动时恢复温层 → 热层
    try:
        with _DB_LOCK, sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT path, kind, version, data FROM ide_cache ORDER BY ts DESC LIMIT ?",
                (_MAX_ENTRIES,),
            ).fetchall()
        with _lock:
            for path, kind, version, data in rows:
                try:
                    entry = _CACHE.setdefault(path, {"version": version, "entries": {}})
                    entry["entries"][kind] = {"data": json.loads(data), "ts": time.time()}
                except (json.JSONDecodeError, TypeError):
                    continue
    except sqlite3.Error:  # 尽力而为（吞错有注释——可追溯）
        pass


def _persist(path: str, kind: str, version: str, data: dict) -> None:
    """温层落盘（后台线程不安全时由调用方持锁——这里独立连接）。"""
    if not _WARM_DB:
        return
    try:
        with _DB_LOCK, sqlite3.connect(_WARM_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ide_cache(path, kind, version, data, ts) "
                "VALUES(?, ?, ?, ?, ?)",
                (path, kind, version, json.dumps(data, ensure_ascii=False), time.time()),
            )
    except sqlite3.Error:  # 尽力而为（吞错有注释——可追溯）
        pass


def file_version(path: str) -> str | None:
    """文件版本指纹：mtime_ns + size + 首尾采样哈希。

    返回 None = 文件不存在/不可读。mtime+size 先粗判（快），哈希保精确（防同 mtime 篡改）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(_SAMPLE_BYTES)
            if st.st_size > _SAMPLE_BYTES * 2:
                f.seek(-_SAMPLE_BYTES, os.SEEK_END)
                tail = f.read(_SAMPLE_BYTES)
            else:
                tail = b""
    except OSError:
        return None
    h = hashlib.blake2b(head + tail, digest_size=8).hexdigest()
    return f"{st.st_mtime_ns}:{st.st_size}:{h}"


def cached(path: str, kind: str) -> dict | None:
    """版本匹配则返回缓存数据，否则 None（缓存失效）。"""
    ver = file_version(path)
    if ver is None:
        return None
    with _lock:
        entry = _CACHE.get(path)
        if entry and entry["version"] == ver and kind in entry["entries"]:
            entry["entries"][kind]["ts"] = time.time()  # LRU 刷新
            return entry["entries"][kind]["data"]
    return None


def store(path: str, kind: str, data: dict) -> None:
    """存缓存（带版本）+ 温层持久化（R3）。"""
    ver = file_version(path)
    if ver is None:
        return
    with _lock:
        entry = _CACHE.setdefault(path, {"version": ver, "entries": {}})
        entry["version"] = ver
        entry["entries"][kind] = {"data": data, "ts": time.time()}
        _evict_lru()
    _persist(path, kind, ver, data)  # 温层落盘（锁外，独立连接）


def invalidate(path: str | None = None) -> None:
    """失效：单个文件或全部（path=None）。"""
    with _lock:
        if path is None:
            _CACHE.clear()
        else:
            _CACHE.pop(path, None)


def _evict_lru() -> None:
    """LRU 淘汰（超上限时逐出最久未用条目）。"""
    while len(_CACHE) > _MAX_ENTRIES:
        oldest_path = None
        oldest_ts = float("inf")
        for p, entry in _CACHE.items():
            for kind, v in entry["entries"].items():
                if v["ts"] < oldest_ts:
                    oldest_ts = v["ts"]
                    oldest_path = p
        if oldest_path is None:
            break
        _CACHE.pop(oldest_path, None)


def stats() -> dict:
    """缓存统计（调试/诊断用）。"""
    with _lock:
        total_entries = sum(len(e["entries"]) for e in _CACHE.values())
        return {"files": len(_CACHE), "entries": total_entries, "max": _MAX_ENTRIES}


def is_cached(path: str, kind: str) -> bool:
    """版本一致且缓存存在（不返回数据——给调用方决定是否省 token 用）。"""
    return cached(path, kind) is not None


# ══════════════ ide_fusion（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_fusion.py — IDE 结果融合进掌握引擎（IDE_ENHANCE_PLAN R6）。

融合三路：
  1. 诊断 → 符号图：bug_scan/quality_scan 问题按符号归属标注（图节点带问题）
  2. IDE 查询 → 教训语料：lsp_query/code_context 高频对象记录为候选教训
  3. 影响面双引擎校验：change_impact（LSP/词级）vs 符号图 callers（tree-sitter）对比
"""


# 符号归属启发式：行号 → 所在函数（tree-sitter 降级：正则 fn/def 扫描）
_FN_RE = re.compile(
    r"^function\s+([A-Za-z_][\w-]*)"                                            # php/ps1（最前——防 ts/js function 分支截断连字符）
    r"|^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"          # rs
    r"|^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\b"                                 # py
    r"|^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("            # go/gd
    r"|^\s*(?:export\s+)?(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*))"  # ts/js
    r"|^\s*(?:static\s+|inline\s+|extern\s+)*(?!return\b|if\b|while\b|for\b)" # c/cpp
    r"[A-Za-z_]\w*\s+([A-Za-z_]\w*)\s*\("
    r"|^\s*(?:public\s+|private\s+|internal\s+|protected\s+)*"
    r"(?:static\s+|virtual\s+|override\s+|async\s+)*"
    r"(?:class|interface|struct|enum)\s+([A-Za-z_]\w*)"                       # cs 类
    r"|^\s*(?:public\s+|private\s+|internal\s+|protected\s+)*"
    r"(?:static\s+|virtual\s+|override\s+|async\s+)*"
    r"[A-Za-z_<>,.]*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{"                      # cs 方法
    r"|^\s*(?:local\s+)?function\s+([A-Za-z_]\w*)"                            # lua
    r"|^\s*([A-Za-z_]\w*)\s*\(\)\s*(?:\{|$)"                                  # sh/bash
    r"|^\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
    r"(?:class|interface|enum)\s+([A-Za-z_]\w*)"                              # java 类
    r"|^\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
    r"[A-Za-z_<>\[\]]*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{|$)"              # java 方法
    r"|^\s*(?:fun\s+)?([A-Za-z_]\w*)\s*\(|^class\s+([A-Za-z_]\w*)"            # kt/kts
    r"|^func\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)|^struct\s+([A-Za-z_]\w*)"  # swift
    r"|^def\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)"                        # rb
    r"|^\s*(?:class|abstract class|mixin|enum)\s+([A-Za-z_]\w*)"                # dart 类
    r"|^\s*(?:Future\s*<[^>]*>\s*|Widget\s+|void\s+|int\s+|String\s+|bool\s+|double\s+)?"
    r"(?!TextButton|ElevatedButton|OutlinedButton|IconButton|FilledButton|"
    r"Column|Row|Container|Text|SizedBox)"
    r"([A-Za-z_]\w*)\s*\("                                                      # dart 函数
)


def annotate_issues(root: str, issues: list[dict]) -> dict:
    """问题列表 → 按文件+符号聚合（诊断标注到符号图）。

    issues: [{file, line, kind, message}]（bug_scan/quality_scan 输出格式）
    返回 {symbol_map: {file#symbol: count}, by_file: {...}, total: n}
    """
    _t0 = time.perf_counter()
    fn_lines: dict[str, list[tuple[int, str]]] = {}
    by_symbol: dict[str, int] = {}
    by_file: dict[str, int] = {}
    by_rule: dict[str, int] = {}  # IDE 增强 100：规则分布（问题类型一眼可见）
    by_sev: dict[str, int] = {}   # IDE 增强 110：严重度分布（优先级一眼可见）

    def load_fn_lines(path: str) -> list[tuple[int, str]]:
        if path in fn_lines:
            return fn_lines[path]
        result: list[tuple[int, str]] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    m = _FN_RE.match(line)
                    if m:
                        name = next((g for g in m.groups() if g), "")
                        if name:
                            result.append((i, name))
        except OSError:  # 尽力而为（吞错可追溯）
            pass
        fn_lines[path] = result
        return result

    for iss in issues:
        path = iss.get("file", "")
        line = iss.get("line", 0)
        by_file[path] = by_file.get(path, 0) + 1
        # IDE 增强 100：规则分布（AI 判断修复优先级——什么类型问题最多）
        rule = str(iss.get("rule") or iss.get("kind") or "unknown")[:40]
        by_rule[rule] = by_rule.get(rule, 0) + 1
        # IDE 增强 110：严重度分布（error/warn/info 计数——优先级一眼可见）
        sev = str(iss.get("severity") or "").lower()
        if sev not in ("error", "warning", "warn", "info", "suggestion"):
            sev = "unknown"
        by_sev[sev] = by_sev.get(sev, 0) + 1
        symbol = "<unknown>"
        cur = None
        for ln, name in load_fn_lines(path):
            if ln <= line:
                cur = name
            else:
                break
        if cur:
            symbol = cur
        key = f"{path}#{symbol}"
        by_symbol[key] = by_symbol.get(key, 0) + 1

    # IDE 增强 290：标注语言分布（问题文件后缀——AI 知道问题
    # 集中在哪些语言，对称扫描工具 languages）
    _a_langs: dict[str, int] = {}
    for _f in by_file:
        _sfx = os.path.splitext(_f)[1].lower().lstrip(".")
        if _sfx:
            _a_langs[_sfx] = _a_langs.get(_sfx, 0) + 1
    return {
        "ok": True,
        "total": len(issues),
        "languages": dict(sorted(_a_langs.items(), key=lambda kv: -kv[1])),
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "severity_counts": dict(sorted(by_sev.items(), key=lambda kv: -kv[1])),
        "symbol_map": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])[:50]),
        # IDE 增强 132：top 符号建议（问题最集中的符号——重点排查入口）
        "top_symbols_advice": [
            {"symbol": sym.split("#")[-1], "file": sym.split("#")[0],
             "issues": cnt}
            for sym, cnt in sorted(by_symbol.items(), key=lambda kv: -kv[1])[:5]
        ],
        # IDE 增强 246：融合耗时（ms——收官；由 server 包装层注入）
        "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
    }


def cross_validate_impact(repo_path: str, symbol: str,
                          lsp_refs: list[str], tree_refs: list[str]) -> dict:
    """双引擎校验：LSP 引用 vs tree-sitter 调用（符号图）。

    lsp_refs/tree_refs 都是文件路径列表。返回交集/差异——两者不一致处是
    潜在漏检或过检（重命名安全性验证用）。
    """
    lsp_set = set(lsp_refs)
    tree_set = set(tree_refs)
    return {
        "ok": True,
        "symbol": symbol,
        "lsp_only": sorted(lsp_set - tree_set),   # LSP 报但图没有 → 可能图缺边
        "tree_only": sorted(tree_set - lsp_set),  # 图报但 LSP 没有 → 可能 LSP 漏
        "both": sorted(lsp_set & tree_set),
        "lsp_count": len(lsp_set),
        "tree_count": len(tree_set),
        "verdict": ("一致" if lsp_set == tree_set
                    else f"差异 {len(lsp_set ^ tree_set)} 处——重命名前需人工确认"),
    }


def impact_via_references(repo_path: str, symbol: str,
                          lsp_refs: list[str]) -> dict:
    """双引擎校验便捷入口（IDE 增强四 2026-08-13）：tree 侧引用数据来自
    ide_references（词级 + 声明判定，注释/字符串排除）——无独立符号图也能跑。

    lsp_refs：LSP 引用（文件路径列表，可空——LSP 不可用时全由 tree 侧提供）。
    返回 cross_validate_impact 的完整结果 + tree 侧引用明细。
    """
    from ide_tools import ide_references
    r = ide_references(repo_path, symbol)
    if not r.get("ok"):
        return {"ok": False, "symbol": symbol,
                "error": r.get("error", "ide_references 失败")}
    tree_files = sorted({ref["file"] for ref in
                         r.get("definitions", []) + r.get("references", [])})
    result = cross_validate_impact(repo_path, symbol,
                                   lsp_refs or [], tree_files)
    result["tree_refs"] = tree_files
    result["definition_count"] = r.get("definition_count", 0)
    result["reference_count"] = r.get("reference_count", 0)
    result["source"] = "tree=ide_references(词级+声明判定，注释/字符串排除)"
    return result


def record_ide_usage(lesson_dir: str, tool: str, target: str, outcome: dict) -> dict:
    """IDE 查询记录 → 教训库候选（高频对象沉淀为教训）。"""
    try:
        os.makedirs(lesson_dir, exist_ok=True)
    except OSError:
        return {"ok": False, "error": f"教训目录不可建: {lesson_dir}"}
    rec = {"tool": tool, "target": target, "outcome": outcome}
    try:
        path = os.path.join(lesson_dir, "ide_usage.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "recorded": rec}


# ══════════════ rx_ide（合并） ══════════════
# SPDX-License-Identifier: MIT
# RX-IDE Lite 入口：默认拉起 pywebview 桌面窗口；--web 仅启动 HTTP 服务。
"""RX-IDE Lite 命令行入口。

用法：
    python rx_ide.py          # pywebview 桌面窗口（缺依赖时自动回退纯 Web）
    python rx_ide.py --web    # 仅 HTTP 服务 http://127.0.0.1:17310/
"""


# 确保项目根在 sys.path 最前，保证 rxide 包可导入
ROOT = _ENGINE_ROOT
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rxide import host  # noqa: E402


def rx_ide_main():
    web_only = "--web" in sys.argv[1:]
    host.start(web_only=web_only)


if __name__ == "__main__":
    rx_ide_main()


# ── 兼容：旧模块名 import 无缝映射到本引擎 ──
import sys as _sys
_sys.modules.setdefault('ide_tools', _sys.modules[__name__])
_sys.modules.setdefault('ide_ui', _sys.modules[__name__])
_sys.modules.setdefault('ide_session', _sys.modules[__name__])
_sys.modules.setdefault('ide_commands', _sys.modules[__name__])
_sys.modules.setdefault('ide_cache', _sys.modules[__name__])
_sys.modules.setdefault('ide_fusion', _sys.modules[__name__])
_sys.modules.setdefault('rx_ide', _sys.modules[__name__])
