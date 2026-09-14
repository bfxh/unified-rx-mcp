# -*- coding: utf-8 -*-
"""tools/impact.py —— ide_impact 影响面（S129 自 tools/lsp.py 平移并加档）。

四档降级链（engine 字段如实标注，引用面 ≠ 调用面分层不混）：
1. **LSP references**（语义级）——LSP 可用时；
2. **名字解析**（S109，解析级）——精确到 import/引用行；
3. **文本全文计数**（S99，含噪声）——最后的兜底；
- **调用面**（S129 新增，独立成段 `calls`，不混进上面任何一档）：
  Rust 调用图（nameres 同一作用域引擎）给出的**调用边**——按定义点
  （to_file/to_line）对齐消歧，回答"谁在调用它"（引用面回答"谁提到了它"）。

调用面不可用（exe 缺失/解析失败）如实缺席，不拖垮主结果；`calls=false` 可关。
"""
import os
import re

from registry import tool
from tools import lsp as _lsp
from tools.lsp_actions import ide_lsp


def _has_local_test(fpath):
    """python 文件名约定的测试覆盖代理（test_<stem>.py，同目录/tests/test/）。
    诚实定界：rust 内联 #[cfg(test)] 不适用。"""
    d = os.path.dirname(fpath)
    stem = os.path.splitext(os.path.basename(fpath))[0]
    for cand in (os.path.join(d, f"test_{stem}.py"),
                 os.path.join(d, f"{stem}_test.py"),
                 os.path.join(d, "tests", f"test_{stem}.py"),
                 os.path.join(d, "test", f"test_{stem}.py")):
        if os.path.isfile(cand):
            return True
    return False


def _ident_at(fp, line, col):
    """文本级兜底的符号来源：取 file:line:col 处的标识符（列优先，列越界取行内
    首个标识符）。返回 None = 该处没有可用符号。"""
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    if not (0 <= line < len(lines)):
        return None
    text = lines[line]
    spans = [(m.start(), m.end(), m.group(0))
             for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text)]
    if not spans:
        return None
    for s, e, w in spans:
        if s <= col < e:
            return w
    return spans[0][2]


def _resolved_impact(real, line, col, lsp_err):
    """S109：解析级中档（LSP 不可用时）——名字解析给出"哪些文件引用了这个定义"。

    精度如实标注：跨文件精确到 import 行（按 to_file/to_line 匹配，别名也覆盖）、
    同文件精确到引用行；属性链/动态特性不计（见 spec/NAMERES.md 边界）。
    不可用（取不到符号 / exe 缺失 / 解析失败）返回 None → 调用方回落文本级。
    """
    sym = _ident_at(real, line, col)
    if not sym:
        return None
    try:
        from tools.scan import _rx_scan_call, _rx_scan_exe
        if _rx_scan_exe() is None:
            return None
    except Exception:
        return None
    root = _lsp._session_root(real)
    rel = os.path.relpath(real, root).replace("\\", "/")
    def_line = int(line) + 1
    dir_out = _rx_scan_call(["resolvedir", root, "300"])
    if not isinstance(dir_out, dict) or dir_out.get("error"):
        return None
    by_file = {}
    for e in dir_out.get("imports") or []:
        if e.get("to_file") == rel and int(e.get("to_line") or 0) == def_line:
            by_file.setdefault(e["file"], []).append(int(e["line"]))
    one = _rx_scan_call(["resolve", real])
    if isinstance(one, dict) and not one.get("error"):
        for e in one.get("edges") or []:
            if e.get("name") == sym and e.get("kind") in ("local", "module"):
                by_file.setdefault(rel, []).append(int(e["line"]))
    files = []
    for f, lines in sorted(by_file.items()):
        full = os.path.join(root, f)
        files.append({"file": full, "refs": len(lines),
                      "lines": sorted(set(lines))[:20],
                      "has_test": _has_local_test(full)})
    untested = [f["file"] for f in files if not f["has_test"]]
    return {"engine": "resolved", "symbol": sym,
            "total_refs": sum(f["refs"] for f in files),
            "files": files, "untested": untested,
            "fallback_reason": f"LSP 不可用：{lsp_err}",
            "note": "解析级（名字解析）：跨文件精确到 import 行（含别名绑定）、"
                    "同文件精确到引用行；属性链与动态特性不计"
                    "（边界见 spec/NAMERES.md）。has_test 为 python "
                    "test_<stem>.py 约定代理"}


def _text_impact(real, line, col, lsp_err):
    """S99：LSP 不可用时的文本级降级——复用 rx-ide 的大小写敏感全文计数
    （ide_rename 预案），只给每文件命中数不给行号（查位置用 locate_edit）。
    精度如实标注：含注释/字符串，受 200 文件帽限制。"""
    sym = _ident_at(real, line, col)
    if not sym:
        return {"error": f"LSP 不可用（{lsp_err}）；文本兜底也取不到符号"
                         f"（{os.path.basename(real)}:{line}:{col} 处无标识符）"}
    root = _lsp._session_root(real)
    from registry import call as _rx
    rr = _rx("ide_rename", {"root": root, "symbol": sym, "new_name": sym,
                            "include_plan": True})
    if not rr.get("ok"):
        return {"error": f"LSP 不可用（{lsp_err}）；文本兜底失败: {rr.get('error')}"}
    res = rr.get("result") or {}
    files = [{"file": p["file"], "refs": p["occurrences"], "lines": [],
              "has_test": _has_local_test(p["file"])}
             for p in (res.get("plan") or [])]
    untested = [f["file"] for f in files if not f["has_test"]]
    import builtins
    import keyword
    warn = ""
    if keyword.iskeyword(sym) or hasattr(builtins, sym):
        warn = "；⚠ 符号是关键字/内建名，命中含大量无关引用，建议指向自定义符号"
    return {"engine": "text", "total_refs": res.get("total_occurrences"),
            "files": files, "untested": untested,
            "symbol": sym, "fallback_reason": f"LSP 不可用：{lsp_err}",
            "note": "文本级兜底（大小写敏感全文计数，含注释/字符串；不给行号——"
                    "查具体位置用 locate_edit；受 200 文件帽限制）。"
                    "has_test 为 python test_<stem>.py 约定代理" + warn}


def _call_impact(real, sym, def_line):
    """S129：调用面（≠引用面）——Rust 调用图的调用边。

    消歧靠**定义点对齐**：边自带 to_file/to_line（被调定义位置），先用
    to_file==rel ∧ to_line==def_line 精确圈定；无命中退化为 to_file+短名
    （同名函数调用边会合并——note 里如实写）。不可用（exe 缺失/报错）→ None。
    """
    try:
        from tools.scan import _rx_scan_exe
        from tools.ide_callgraph import _rx_callgraph
        if _rx_scan_exe() is None:
            return None
    except Exception:
        return None
    root = _lsp._session_root(real)
    rel = os.path.relpath(real, root).replace("\\", "/")
    raw = _rx_callgraph(root, 300)
    if not isinstance(raw, dict) or raw.get("error"):
        return None
    edges = [e for e in (raw.get("edges") or []) if isinstance(e, dict)]
    picked, exact = [], True
    for e in edges:
        if e.get("to_file") != rel or (e.get("callee") or "").rsplit(".", 1)[-1] != sym:
            continue
        if int(e.get("to_line") or 0) == def_line:
            picked.append(e)
    if not picked:
        exact = False
        for e in edges:
            if e.get("to_file") == rel and (e.get("callee") or "").rsplit(".", 1)[-1] == sym:
                picked.append(e)
    seen, callers = set(), []
    for e in picked:
        key = (e.get("file"), e.get("line"))
        if key in seen or not e.get("file"):
            continue
        seen.add(key)
        callers.append({"file": os.path.join(root, str(e.get("file")).replace("/", os.sep)),
                        "line": e.get("line"),
                        "caller": e.get("caller") or "<module>"})
    callers.sort(key=lambda x: (x["file"], x["line"] or 0))
    return {"engine": "rust:nameres-callgraph", "call_sites": len(callers),
            "callers": callers[:50],
            "def_line_aligned": exact,
            "note": ("调用面：仅含调用图可解析的调用边（不可解析调用如实缺席）；"
                     "引用≠调用——提到符号未必调用它。"
                     + ("" if exact else "；⚠ 定义点未精确对齐，按文件+短名圈定，"
                                         "同名函数的调用边可能合并"))}


@tool("ide_impact", "影响面分析：符号 → 引用按文件聚合 + 测试覆盖标注（改前先看裸奔文件）；engine 三级如实标注（LSP→名字解析→文本）；另附 calls 调用面段",
      "ide",
      {"type": "object",
       "properties": {
           "file": {"type": "string", "description": "符号所在文件（沙盒内绝对路径）"},
           "line": {"type": "integer", "description": "0-based 行"},
           "col": {"type": "integer", "description": "0-based 字符列"},
           "include_decl": {"type": "boolean", "description": "是否含声明处（默认 true）"},
           "calls": {"type": "boolean",
                     "description": "附调用面段（默认 true；exe 缺失时如实缺席）"},
       },
       "required": ["file"]})
def ide_impact(file, line=0, col=0, include_decl=True, calls=True):
    res = _impact_core(file, line, col, include_decl)
    if calls and isinstance(res, dict) and "error" not in res:
        try:
            real = _lsp._resolve_in_sandbox(file)
            sym = res.get("symbol") or _ident_at(real, int(line), int(col))
            if sym:
                c = _call_impact(real, sym, int(line) + 1)
                if c is not None:
                    res["calls"] = c
        except Exception:
            pass                     # 调用面是增强——绝不拖垮主结果（fail-open 有界）
    return res


def _impact_core(file, line, col, include_decl):
    """原有三级降级主体（S129 平移不改语义）。"""
    lsp_err = None
    r = None
    try:
        r = ide_lsp("references", file=file, line=line, col=col,
                    include_decl=include_decl)
    except Exception as e:                       # S99：会话起不来不再抛穿
        lsp_err = f"{type(e).__name__}: {e}"
    if r is not None and not r.get("error"):
        groups = {}
        for ref in r.get("references") or []:
            groups.setdefault(ref["file"], []).append(ref["line"])
        files = [{"file": f, "refs": len(ls), "lines": ls[:20],
                  "has_test": _has_local_test(f)}
                 for f, ls in sorted(groups.items())]
        untested = [f["file"] for f in files if not f["has_test"]]
        return {"engine": r.get("engine"), "total_refs": r.get("total"),
                "files": files, "untested": untested,
                "note": "has_test 为 python test_<stem>.py 约定代理；"
                        "rust 内联 #[cfg(test)] 不适用"}
    if r is not None and r.get("error"):
        lsp_err = str(r.get("error"))
    # S99/S109：LSP 不可用 → 三级降级（沙盒门与 LSP 路径同款）：
    # 解析级（名字解析，精确到 import/引用行）→ 文本级（全文计数，含噪声）
    try:
        real = _lsp._resolve_in_sandbox(file)
    except PermissionError as e:
        return {"error": str(e)}
    try:
        res = _resolved_impact(real, int(line), int(col), lsp_err)
    except Exception:
        res = None
    if res is not None:
        return res
    return _text_impact(real, int(line), int(col), lsp_err)
