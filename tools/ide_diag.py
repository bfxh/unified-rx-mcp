# -*- coding: utf-8 -*-
"""tools/ide_diag.py —— 统一诊断面（S48 拆分；S130：linter 探测 + 信号缺席如实上报）。

诊断源（统一形状 `{source, file, line(1-based), col, severity, message}`）：
- **LSP 诊断**（语义级，经 ide_lsp）；- **cargo clippy**（Rust lint，经 ide_build）；
- **Python 外部 linter 探测薄壳（S130 / CONSOLIDATION §四 P2-A）**：
  `ruff`（优先，JSON 输出，`--no-cache`）→ 无 ruff 退回 `pyflakes`；
  `mypy` 独立跑（类型面，缓存目录钉到 TEMP）。**装了就用、没装/超时如实记入
  `skipped` 列表**——能力缺席看得见，不静默（零 pip 依赖红线不破：外调不内嵌）。

执行类工具（S130 修正）：本工具会**执行** cargo clippy / 外部 linter 子进程，
挂 `requires_auth` 门（与 ide_build/ide_test 同类）；内层 `registry.call` 显式携带
`__authorized: True` 传递外层已确认的授权。修正背景：此前无门导致 clippy 透镜在
生产路径上被授权门拒绝、又被 `except` 静默吞成空信号（`engine=none, total=0`
S130 实测实锤）——现在拒绝/失败一律进入 `skipped`。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import registry  # 显式导入：_lsp_file_diags/_clippy_diags 依赖 registry.call（S55：拆分后缺此导入，
                 # NameError 被 except Exception 静默吞掉，LSP+clippy 信号全空）
from registry import tool
from tools.fs import _resolve as _fs_resolve

_SEV_LSP = {"error": "error", "warning": "warning", "info": "info", "hint": "hint"}

_LANG_BY_EXT = {".py": "python", ".rs": "rust"}


def _which(name):
    """能力探测点（独立成函数：测试可替换，不碰真 PATH）。"""
    return shutil.which(name)


def _rel_to(path, fp):
    """诊断文件路径 → 相对项目根（统一 "/" 分隔；项目外路径原样）。"""
    try:
        full = os.path.abspath(fp)
        if os.path.commonpath([os.path.abspath(path), full]) == os.path.abspath(path):
            return os.path.relpath(full, path).replace("\\", "/")
    except (ValueError, OSError):
        pass
    return str(fp).replace("\\", "/")


def _lsp_file_diags(path, rel):
    """单文件 LSP 诊断 → (统一形状列表, 引擎名, skip 原因)。"""
    fp = os.path.abspath(os.path.join(path, rel.replace("/", os.sep)))
    if not os.path.isfile(fp):
        return [], None, None
    lang = _LANG_BY_EXT.get(os.path.splitext(fp)[1].lower())
    if not lang:
        return [], None, None
    try:
        r = registry.call("ide_lsp", {"action": "diagnostics", "file": fp})
        if not r.get("ok"):
            return [], None, None       # LSP 不可用是能力缺席，不算本次 skip（ide_lsp status 可查）
        res = r.get("result") or {}
        diags = [{"source": d.get("source") or f"{lang}-lsp", "file": rel,
                  "line": int(d.get("line") or 0) + 1, "col": 0,
                  "severity": _SEV_LSP.get(str(d.get("severity")), "warning"),
                  "message": (d.get("message") or "")[:200]}
                 for d in res.get("diagnostics") or []]
        return diags, (f"{lang}-lsp" if diags else None), None
    except Exception:
        return [], None, None           # LSP 不可用 → 如实跳过该信号


def _clippy_diags(path):
    """clippy 诊断 → (统一形状列表, 引擎名, skip 原因)。

    S130：内层调用显式携带 __authorized（外层门已确认授权）；拒绝/失败
    进入 skip 原因——不再静默吞（S55 类缺陷的授权门变体）。
    """
    if not os.path.isfile(os.path.join(path, "Cargo.toml")):
        return [], None, None           # 非 Rust 项目：无此信号，不算 skip
    try:
        r = registry.call("ide_build", {"path": path, "action": "lint",
                                        "__authorized": True})
        if not r.get("ok"):
            return [], None, f"clippy 跳过: {str(r.get('error'))[:120]}"
        res = r.get("result") or r
        diags = [{"source": "clippy",
                  "file": os.path.relpath(d["file"], path).replace("\\", "/"),
                  "line": d["line"], "col": d.get("col", 0),
                  "severity": d["level"], "message": d["msg"][:200]}
                 for d in (res.get("warnings") or []) + (res.get("errors") or [])]
        return diags, ("clippy" if diags else None), None
    except Exception as e:
        return [], None, f"clippy 异常: {type(e).__name__}: {str(e)[:120]}"


# ---------- S130：Python 外部 linter 探测薄壳（解析器独立成函数，可单测） ----------

def _parse_ruff_json(out):
    """ruff check --output-format=json → 统一形状。E9*/F82*（语法级）= error。"""
    diags = []
    try:
        items = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return diags
    for it in items or []:
        if not isinstance(it, dict):
            continue
        loc = it.get("location") or {}
        code = str(it.get("code") or "")
        sev = "error" if code.startswith(("E9", "F82")) else "warning"
        diags.append({"source": "ruff",
                      "file": str(it.get("filename") or ""),
                      "line": int(loc.get("row") or 0),
                      "col": int(loc.get("column") or 0),
                      "severity": sev,
                      "message": f"{code}: {(it.get('message') or '')}"[:200]})
    return diags


_RE_PYFLAKES = re.compile(r"^(.*?):(\d+)(?::(\d+))?:\s+(.*)$")


def _parse_pyflakes(out):
    """pyflakes 文本输出 → 统一形状（无级别词，一律 warning）。"""
    diags = []
    for line in (out or "").splitlines():
        m = _RE_PYFLAKES.match(line.strip())
        if not m:
            continue
        fp, ln, col, msg = m.groups()
        diags.append({"source": "pyflakes", "file": fp, "line": int(ln),
                      "col": int(col) if col else 0, "severity": "warning",
                      "message": msg[:200]})
    return diags


_RE_MYPY = re.compile(r"^(.*?):(\d+):(?:\s*(\d+):)?\s*(error|warning|note):\s*(.*)$")


def _parse_mypy(out):
    """mypy 文本输出 → 统一形状（note → info，与 LSP 严重度对齐）。"""
    diags = []
    for line in (out or "").splitlines():
        m = _RE_MYPY.match(line.strip())
        if not m:
            continue
        fp, ln, col, sev, msg = m.groups()
        diags.append({"source": "mypy", "file": fp, "line": int(ln),
                      "col": int(col) if col else 0,
                      "severity": {"error": "error", "warning": "warning",
                                   "note": "info"}[sev],
                      "message": msg[:200]})
    return diags


def _run(cmd, timeout, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, shell=False, env=env)


def _python_linter_diags(path):
    """(统一形状列表, 引擎名列表, skip 原因列表)。ruff 优先，无则 pyflakes；mypy 独立。"""
    diags, engines, skipped = [], [], []
    linters = []
    if _which("ruff"):
        linters.append("ruff")
    elif _which("pyflakes"):
        linters.append("pyflakes")
    else:
        skipped.append("ruff/pyflakes 均未安装——装上任一即启用 Python lint 面")
    if _which("mypy"):
        linters.append("mypy")
    else:
        skipped.append("mypy 未安装——装上即启用类型检查面")
    for name in linters:
        try:
            if name == "ruff":
                cp = _run(["ruff", "check", "--output-format=json", "--no-cache", path],
                          120)
                got = _parse_ruff_json(cp.stdout or "")
            elif name == "pyflakes":
                cp = _run(["pyflakes", path], 120)
                got = _parse_pyflakes(cp.stdout or "")
            else:                        # mypy
                env = dict(os.environ)
                env["MYPY_CACHE_DIR"] = os.path.join(tempfile.gettempdir(),
                                                     "rx-mypy-cache")
                cp = _run(["mypy", "--no-color-output", "--no-error-summary",
                           "--no-incremental", "--ignore-missing-imports", path],
                          300, env=env)
                got = _parse_mypy(cp.stdout or "")
            for d in got:
                d["file"] = _rel_to(path, d["file"])
            diags.extend(got)
            engines.append(name)
        except subprocess.TimeoutExpired:
            skipped.append(f"{name} 超时（本次信号缺席）")
        except OSError as e:
            skipped.append(f"{name} 启动失败: {str(e)[:120]}")
    return diags, engines, skipped


@tool("ide_diagnostics",
      "统一诊断通道（执行类需授权）：LSP 诊断 + cargo clippy + Python 外部 linter 探测"
      "（ruff/pyflakes + mypy，装了就用）聚合（同一形状，severity 归一，行号 1-based）"
      "——修复循环/agent 直接消费；能力缺席/失败如实进 skipped 不静默", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（沙盒内）"},
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "相对路径列表（LSP 诊断目标；缺省跳过 LSP）"},
           "include_lint": {"type": "boolean",
                            "description": "含 cargo clippy（Cargo.toml 存在时，默认 true）"},
           "include_linters": {"type": "boolean",
                               "description": "含 Python 外部 linter 探测（默认 true）"},
           "__authorized": {"type": "boolean",
                            "description": "写/执行操作授权确认（必须 true）"},
       },
       "required": ["path", "__authorized"]},
      requires_auth=True)
def ide_diagnostics(path, files=None, include_lint=True, include_linters=True,
                    timeout=600, __authorized=False):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    diags, engines, skipped = [], [], []
    for rel in (files or [])[:3]:
        d, eng, skip = _lsp_file_diags(path, rel)
        diags.extend(d)
        if eng:
            engines.append(eng)
        if skip:
            skipped.append(skip)
    if include_lint:
        d, eng, skip = _clippy_diags(path)
        diags.extend(d)
        if eng:
            engines.append(eng)
        if skip:
            skipped.append(skip)
    if include_linters:
        d, engs, skips = _python_linter_diags(path)
        diags.extend(d)
        engines.extend(engs)
        skipped.extend(skips)
    errors = [d for d in diags if d["severity"] == "error"]
    return {"engine": "+".join(engines) or "none", "total": len(diags),
            "errors": len(errors), "diagnostics": diags[:200],
            "skipped": skipped}
