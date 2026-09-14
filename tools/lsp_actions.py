# -*- coding: utf-8 -*-
"""tools/lsp_actions.py —— ide_lsp 动作分发（S129 自 tools/lsp.py 平移）。

拆分纪律（CONSOLIDATION §三 P1）：只动归属不动语义——动作分发与客户端核心
（服务器探测/会话泵/文本编辑）分家；本模块对 lsp 客户端状态一律**模块属性访问**
（`_lsp._SESSIONS` 等），保证既有测试对 `tools.lsp` 的 monkeypatch 面继续生效
（test_s60 直接替换 `_SESSIONS`、test_s99 替换 `_module_available`）。

`ide_lsp` 是混合读写工具：读动作开放，rename_apply 落盘在 handler 内自查
`__authorized`（S77 manual_gate，注册声明不变）。
"""
import os

from registry import tool
from tools import lsp as _lsp


@tool("ide_lsp", "真 LSP 语义查询（rust-analyzer/pylsp）：definition/references/hover/symbols/diagnostics/rename_plan/rename_apply——apply 落盘需授权", "ide",
      {"type": "object",
       "properties": {
           "action": {"type": "string",
                      "description": "status/definition/references/hover/document_symbols/"
                                     "diagnostics/rename_plan/rename_apply/shutdown"},
           "file": {"type": "string", "description": "目标文件（沙盒内绝对路径）"},
           "line": {"type": "integer", "description": "0-based 行"},
           "col": {"type": "integer", "description": "0-based 字符列"},
           "new_name": {"type": "string", "description": "rename_plan 用"},
           "include_decl": {"type": "boolean", "description": "references 是否含声明处"},
       },
       "required": ["action"]},
      manual_gate=True)  # S77：单工具混合读写——读开放，rename_apply 在 handler 内自查 __authorized
def ide_lsp(action, file=None, line=0, col=0, new_name=None, include_decl=True,
            __authorized=False):
    _lsp.reap_idle()                                   # 接线空闲回收（此前是死代码）
    if action == "status":
        out = {}
        for lang, spec in _lsp._LSP_SERVERS.items():
            cmd = spec["cmd"]() or [""]
            found = _lsp._detect_exe(spec)
            entry = {"label": spec["label"], "detected": bool(found),
                     "exe": found,
                     "sessions_alive": sum(1 for (l, _), (s,) in _lsp._SESSIONS.items()
                                           if l == lang)}
            if not found:
                entry["reason"] = f"{' '.join(cmd)} 不可用（未安装/不在 PATH/模块缺失）"
            out[lang] = entry
        return {"servers": out,
                "note": "definition/references 为语义级精确结果（相较文本级 ide 工具）"}

    try:
        real = _lsp._resolve_in_sandbox(file)
    except PermissionError as e:
        return {"error": str(e)}
    lang = _lsp._LANG_BY_EXT.get(os.path.splitext(real)[1].lower())
    if not lang:
        return {"error": f"不支持的扩展名 {os.path.splitext(real)[1]}；仅 .rs/.py 已接线"}
    root = _lsp._session_root(real)

    try:
        if action == "shutdown":
            killed = 0
            for (l, _), (s,) in list(_lsp._SESSIONS.items()):
                if l == lang:
                    s.stop()
                    _lsp._SESSIONS.pop((l, _), None)
                    killed += 1
            return {"ok": True, "stopped": killed}

        if action == "document_symbols":
            sess = _lsp._get_session(lang, root)
            sess.ensure_open(real)
            r = _lsp._call_ready(sess, "textDocument/documentSymbol",
                                 {"textDocument": {"uri": _lsp._as_uri(real)}})
            flat = []

            def walk(items):
                for it in items or []:
                    o = _lsp._sanitize(it)
                    flat.append(o)
                    walk(it.get("children"))
            walk(r if isinstance(r, list) else [])
            return {"engine": f"{lang}-lsp", "total": len(flat), "symbols": flat[:200]}

        if action == "diagnostics":
            sess = _lsp._get_session(lang, root)
            sess.ensure_open(real)
            # S50：文件落盘后内容变了 → 先推 didChange（增量），再泵新诊断
            sess.refresh_if_stale(real)
            # 发布式诊断靠推不靠拉：必须持续泵管道才收得到通知
            sess.pump(6.0)
            items = sess.diagnostics.get(_lsp._as_uri(real), [])
            ds = [{"severity": {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
                       d.get("severity"), str(d.get("severity"))),
                   "line": d.get("range", {}).get("start", {}).get("line"),
                   "message": (d.get("message") or "")[:200],
                   "source": d.get("source")}
                  for d in items[:100]]
            return {"engine": f"{lang}-lsp", "total": len(ds), "diagnostics": ds}

        sess, uri, pos = _lsp._locate(lang, real, int(line), int(col))
        tdpos = {"textDocument": {"uri": uri}, "position": pos}

        if action == "definition":
            r = _lsp._call_ready(sess, "textDocument/definition", dict(tdpos))
            locs = r if isinstance(r, list) else ([r] if r else [])
            return {"engine": f"{lang}-lsp",
                    "locations": [_lsp._sanitize(x) for x in locs][:20], "total": len(locs)}

        if action == "references":
            r = _lsp._call_ready(sess, "textDocument/references",
                                 {**tdpos, "context": {"includeDeclaration": bool(include_decl)}})
            out = [{"file": _lsp._uri_path(x["uri"]), "line": x["range"]["start"]["line"]}
                   for x in (r or [])[:200]]
            return {"engine": f"{lang}-lsp", "total": len(out), "references": out}

        if action == "hover":
            r = _lsp._call_ready(sess, "textDocument/hover", dict(tdpos))
            return {"engine": f"{lang}-lsp", "result": _lsp._sanitize(r) if r else None}

        if action == "rename_plan":
            if not new_name:
                return {"error": "rename_plan 需要 new_name"}
            r = _lsp._call_ready(sess, "textDocument/rename", {**tdpos,
                                                               "newName": new_name})
            if not r:
                return {"engine": f"{lang}-lsp", "plan": [], "note": "无可改引用"}
            plan = []
            for uriedit in (r.get("changes") or {}).items():
                u, edits = uriedit
                for ed in edits[:50]:
                    s = ed.get("range", {}).get("start", {})
                    plan.append({"file": _lsp._uri_path(u), "line": s.get("line"),
                                 "newText": (ed.get("newText") or "")[:60]})
            for wd in (r.get("documentChanges") or []):
                u = (wd.get("textDocument") or {}).get("uri")
                for ed in (wd.get("edits") or [])[:50]:
                    s = ed.get("range", {}).get("start", {})
                    plan.append({"file": _lsp._uri_path(u), "line": s.get("line"),
                                 "newText": (ed.get("newText") or "")[:60]})
            return {"engine": f"{lang}-lsp", "applied": False,
                    "note": "预案不落盘（写盘归宿主）", "total": len(plan), "plan": plan[:80]}

        if action == "rename_apply":
            # R3：rename 落盘——把 WorkspaceEdit 应用到沙盒内文件（逐文件 _resolve 防逃逸）
            if not new_name:
                return {"error": "rename_apply 需要 new_name"}
            if __authorized is not True:
                return {"error": "PermissionError: rename_apply 落盘需要授权："
                                 "参数加 __authorized: true 确认后重试"}
            r = _lsp._call_ready(sess, "textDocument/rename", {**tdpos,
                                                               "newName": new_name})
            if not r:
                return {"engine": f"{lang}-lsp", "applied": False,
                        "total": 0, "files": [], "note": "无可改引用"}
            by_file, rejected = {}, []
            for u, eds in (r.get("changes") or {}).items():
                if not str(u).lower().startswith("file:"):
                    rejected.append({"uri": str(u)[:60],
                                     "error": "非 file: uri——拒绝落盘"})
                    continue
                by_file.setdefault(_lsp._uri_path(u), []).extend(eds)
            for wd in (r.get("documentChanges") or []):
                u = (wd.get("textDocument") or {}).get("uri") or ""
                if not str(u).lower().startswith("file:"):
                    rejected.append({"uri": str(u)[:60],
                                     "error": "非 file: uri——拒绝落盘"})
                    continue
                by_file.setdefault(_lsp._uri_path(u), []).extend(wd.get("edits") or [])
            results, total = [], 0
            for fpath, eds in by_file.items():
                try:
                    real = _lsp._resolve_in_sandbox(fpath)
                except PermissionError as e:
                    results.append({"file": fpath, "error": str(e)})
                    continue
                try:
                    with open(real, "r", encoding="utf-8", errors="replace",
                              newline="") as f:
                        src = f.read()
                except OSError as e:
                    results.append({"file": real, "error": str(e)})
                    continue
                new_src, n = _lsp._apply_text_edits(src, eds)
                with open(real, "w", encoding="utf-8", newline="") as f:
                    f.write(new_src)
                sess.notify_change(real)       # 会话内文档同步，防陈旧诊断
                total += n
                results.append({"file": real, "edits": n})
            return {"engine": f"{lang}-lsp", "applied": True, "total": total,
                    "files": results,
                    **({"rejected": rejected} if rejected else {})}

        return {"error": f"未知 action: {action}"}
    except FileNotFoundError as e:
        return {"error": f"LSP 服务器未检出: {e}"}
    except RuntimeError as e:
        return {"error": str(e), "hint": "首查可能因建索引超时；重试或放宽预算"}
    except Exception as e:                                  # noqa: BLE001 失败语义统一
        return {"error": f"{type(e).__name__}: {e}"}
