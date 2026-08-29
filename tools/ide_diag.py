# -*- coding: utf-8 -*-
"""tools/ide_diag.py —— 统一诊断面（S48 拆分）。"""
import os

import registry  # 显式导入：_lsp_file_diags/_clippy_diags 依赖 registry.call（S55：拆分后缺此导入，
                 # NameError 被 except Exception 静默吞掉，LSP+clippy 信号全空）
from registry import tool
from tools.fs import _resolve as _fs_resolve

_SEV_LSP = {"error": "error", "warning": "warning", "info": "info", "hint": "hint"}

_LANG_BY_EXT = {".py": "python", ".rs": "rust"}


def _lsp_file_diags(path, rel):
    """单文件 LSP 诊断 → 统一形状列表（异常=无信号）。"""
    fp = os.path.abspath(os.path.join(path, rel.replace("/", os.sep)))
    if not os.path.isfile(fp):
        return [], None
    lang = _LANG_BY_EXT.get(os.path.splitext(fp)[1].lower())
    if not lang:
        return [], None
    try:
        r = registry.call("ide_lsp", {"action": "diagnostics", "file": fp})
        res = r.get("result") or {}
        diags = [{"source": d.get("source") or f"{lang}-lsp", "file": rel,
                  "line": int(d.get("line") or 0) + 1, "col": 0,
                  "severity": _SEV_LSP.get(str(d.get("severity")), "warning"),
                  "message": (d.get("message") or "")[:200]}
                 for d in res.get("diagnostics") or []]
        return diags, (f"{lang}-lsp" if diags else None)
    except Exception:
        return [], None                 # LSP 不可用 → 如实跳过该信号


def _clippy_diags(path):
    """clippy 诊断 → 统一形状列表（异常=无信号）。"""
    if not os.path.isfile(os.path.join(path, "Cargo.toml")):
        return [], None
    try:
        r = registry.call("ide_build", {"path": path, "action": "lint"})
        res = r.get("result") or r
        diags = [{"source": "clippy",
                  "file": os.path.relpath(d["file"], path).replace("\\", "/"),
                  "line": d["line"], "col": d.get("col", 0),
                  "severity": d["level"], "message": d["msg"][:200]}
                 for d in (res.get("warnings") or []) + (res.get("errors") or [])]
        return diags, ("clippy" if diags else None)
    except Exception:
        return [], None


@tool("ide_diagnostics", "统一诊断通道：LSP 诊断 + cargo clippy 聚合（同一形状，"
      "severity 归一，行号 1-based）——修复循环/agent 直接消费", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（沙盒内）"},
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "相对路径列表（LSP 诊断目标；缺省跳过 LSP）"},
           "include_lint": {"type": "boolean",
                            "description": "含 cargo clippy（Cargo.toml 存在时，默认 true）"},
       },
       "required": ["path"]})
def ide_diagnostics(path, files=None, include_lint=True, timeout=600):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    diags, engines = [], []
    for rel in (files or [])[:3]:
        d, eng = _lsp_file_diags(path, rel)
        diags.extend(d)
        if eng:
            engines.append(eng)
    if include_lint:
        d, eng = _clippy_diags(path)
        diags.extend(d)
        if eng:
            engines.append(eng)
    errors = [d for d in diags if d["severity"] == "error"]
    return {"engine": "+".join(engines) or "none", "total": len(diags),
            "errors": len(errors), "diagnostics": diags[:200]}
