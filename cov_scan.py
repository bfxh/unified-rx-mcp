#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""cov_scan —— 代码覆盖率分析（阶段3，定位"从未执行的代码"=隐形炸弹）。

两级模式：
  static（默认，零依赖，确定性）：
    - Python AST：全库符号引用表 → 从未被引用的顶层函数/类/常量
      （死代码候选）+ 未使用的 import——"选择性插桩"的静态等价
    - 排除：__init__/main/下划线开头/typing 导入/测试文件自身
  dynamic（opt-in，需 coverage.py）：
    - subprocess `coverage run -m pytest` → `coverage report --skip-covered`
    - 输出未覆盖文件/行 TOP + 建议补测点
    失败自动降级 static（cov 不可用/无测试时诚实降级，不假装覆盖数据）

跨语言：Python 全量 AST；Rust/其他文件只统计（提示可用 vuln_scan/llvm-cov）。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys


# 忽略的符号前缀/名字（框架入口/魔法方法）
_IGNORE_PREFIX = ("_", "test_", "test")
_IGNORE_NAMES = {"main", "setup", "teardown", "app", "server", "create_app",
                 "get_app", "run", "start", "main_loop"}


def _iter_py_files(root: str, limit: int = 2000):
    """递归收集 .py 文件（跳过常见噪音目录）。"""
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "target",
            "vendor", ".pytest_cache", "build", "dist", ".unified-rx-index"}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)
                count += 1
                if count >= limit:
                    return


def _collect_symbols(files: list[str]) -> tuple[dict, dict]:
    """两遍：定义表 {symbol: [file:line]} + 引用表 {symbol: count}。"""
    defined: dict[str, list[dict]] = {}
    used: dict[str, int] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        # 定义（顶层 def/class/Assign/AsyncFunctionDef）
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.setdefault(node.name, []).append(
                    {"file": path, "line": node.lineno})
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.setdefault(t.id, []).append(
                            {"file": path, "line": node.lineno})
        # 赋值目标集合（Assign/AnnAssign/AugAssign 的 target 不算引用）
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                tgt = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in tgt:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            targets.add(sub.id)
        # 引用（全树 Name/Attribute 的 id，排除赋值目标）
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if node.id not in targets:
                    used[node.id] = used.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute):
                used[node.attr] = used.get(node.attr, 0) + 1
            elif isinstance(node, ast.Import):
                for a in node.names:
                    used[a.asname or a.name.split(".")[0]] = \
                        used.get(a.asname or a.name.split(".")[0], 0) + 1
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    used[a.asname or a.name] = \
                        used.get(a.asname or a.name, 0) + 1
    return defined, used


def _unused_imports(files: list[str]) -> list[dict]:
    """未使用的 import（模块级导入名未被引用）。"""
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.asname or a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [a.asname or a.name for a in node.names]
            for n in names:
                if n not in used and not n.startswith("_"):
                    out.append({"file": path, "line": node.lineno,
                                "symbol": n, "kind": "unused_import"})
    return out


def cov_scan(path: str, mode: str = "static", limit: int = 2000) -> dict:
    """覆盖率/死代码扫描主入口。mode: static | dynamic | auto"""
    if not os.path.isdir(path):
        return {"ok": False, "error": f"路径不存在: {path}"}
    files = list(_iter_py_files(path, limit))
    result: dict = {"ok": True, "path": path, "mode": mode,
                    "py_files": len(files)}

    # ── 动态覆盖（coverage.py，失败诚实降级） ─────────────────
    if mode in ("dynamic", "auto"):
        try:
            import coverage  # noqa: F401
            cov = _run_coverage(path)
            if cov is not None:
                result.update(cov)
                result["mode"] = "dynamic"
                return result
        except ImportError:
            pass
        if mode == "dynamic":
            result["mode"] = "static"
            result["degraded"] = "coverage.py 不可用或无 pytest 测试——降级静态分析"

    # ── 静态死代码（零依赖） ─────────────────────────────────
    defined, used = _collect_symbols(files)
    dead = []
    for sym, locs in defined.items():
        if sym in _IGNORE_NAMES or sym.startswith(_IGNORE_PREFIX):
            continue
        if used.get(sym, 0) == 0:  # 零真实引用（定义处不算）
            for loc in locs[:3]:
                dead.append({"file": loc["file"], "line": loc["line"],
                             "symbol": sym, "kind": "never_referenced"})
    unused_imp = _unused_imports(files)
    result["dead_code"] = dead[:100]
    result["unused_imports"] = unused_imp[:100]
    result["dead_count"] = len(dead)
    result["unused_import_count"] = len(unused_imp)
    result["hint"] = ("'从未被执行的代码'是隐形炸弹——dead_code 为从未被引用的"
                      "顶层符号，建议确认后删除或补测试；动态覆盖用 mode=dynamic")
    return result


def _run_coverage(root: str) -> dict | None:
    """coverage run -m pytest → report（未覆盖 TOP）。耗时操作，超时保护。"""
    try:
        import coverage
        # 找测试入口
        test_cmd = ["python", "-m", "pytest", root, "-q", "--no-header"]
        r = subprocess.run(["python", "-m", "coverage", "run", "--branch",
                            "-m", "pytest", root, "-q", "--no-header",
                            "-x", "--timeout=120"],
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        if r.returncode not in (0, 1):  # 1=有测试失败（仍可出覆盖报告）
            return None
        rep = subprocess.run(["python", "-m", "coverage", "report",
                              "--skip-covered", "--format=json"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
        if rep.returncode != 0:
            return None
        data = json.loads(rep.stdout)
        files_ = data.get("files", {})
        total = data.get("totals", {})
        uncovered = []
        for fname, finfo in files_.items():
            s = finfo.get("summary", {})
            missing = finfo.get("missing_lines", [])
            if s.get("missing_lines", 0) > 0:
                uncovered.append({"file": fname,
                                  "percent_covered": s.get("percent_covered", 0),
                                  "missing": len(missing),
                                  "missing_lines": missing[:20]})
        uncovered.sort(key=lambda x: x["percent_covered"])
        return {
            "dynamic": True,
            "coverage_percent": total.get("percent_covered", 0),
            "covered_lines": total.get("covered_lines", 0),
            "missing_lines": total.get("missing_lines", 0),
            "uncovered_files_top": uncovered[:20],
            "hint": "uncovered_files_top 为覆盖最差文件——优先补测",
        }
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":  # CLI 调试入口
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    mode = sys.argv[2] if len(sys.argv) > 2 else "static"
    print(json.dumps(cov_scan(path, mode), ensure_ascii=False, indent=1))
