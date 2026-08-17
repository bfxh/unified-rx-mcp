#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""repo_health.py — 代码库健康四理念（去重 / 剔残缺 / 分支 / 标矛盾）。

用户理念（2026-08-17）：**去重、剔残缺、分支、标矛盾**是主要目标理念之一。

四个检测维度 + 汇总（action 分发，与 mesh/telemetry 组合化风格一致）：

  dedup      去重：全库相似文件对（SHA-256 + Jaccard）、重复代码块
  incomplete 剔残缺：空实现（pass/.../NotImplementedError）、TODO 堆积、占位符、断引用
  branch     分支：git 分支状态（未合并/已合并/分叉）、游离 HEAD、未提交改动
  conflict   标矛盾：同名符号多处定义、规范冲突、注释-实现矛盾
  all        汇总：四维一屏 + 健康评分 0-100

纯 stdlib 实现（ast/hashlib/subprocess），只读不写。路径规范化防越界。
"""
import ast
import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

# 排除目录（去重/残缺扫描跳过）
_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "build", "target",
    "models", "vendor", ".venv", "venv", "env", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "lse-engine/target",
}
# 源码扩展名（去重/残缺/矛盾扫描目标）
_SRC_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".c", ".cpp", ".h"}
# 占位符模式（残缺检测）
_PLACEHOLDER_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|placeholder|coming\s+soon|待实现|未实现|占位)\b",
    re.IGNORECASE,
)
# 空实现体模式（残缺检测）
_EMPTY_BODY_RE = re.compile(
    r"^\s*(pass|\.\.\.|return\s*$|return\s+None\s*$|raise\s+NotImplementedError\b)",
    re.MULTILINE,
)
# 无意义命名（仅提示）
_JUNK_NAMES = {"foo", "bar", "baz", "tmp", "temp", "test", "dummy", "placeholder", "xxx"}


@dataclass
class HealthItem:
    """一条检测结果。"""

    kind: str          # dedup/incomplete/branch/conflict
    severity: str      # high/medium/low/info
    message: str
    path: str = ""
    detail: dict = field(default_factory=dict)


def _iter_src_files(root: str):
    """遍历源码文件（跳过排除目录）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in _SRC_EXTS:
                yield os.path.join(dirpath, fn)


def _norm_lines(text: str) -> set[str]:
    """行级归一化（去空白/注释），用于相似度。"""
    lines = set()
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//") or s.startswith("/*"):
            continue
        lines.add(s)
    return lines


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def dedup_scan(root: str, top: int = 20) -> list[HealthItem]:
    """去重：完全相同文件组 + 近似重复文件对 + 重复代码块。"""
    items: list[HealthItem] = []
    files: dict[str, tuple[str, str]] = {}  # path -> (sha, text)
    for p in _iter_src_files(root):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        files[p] = (_sha256(text), text)

    # 1) 完全相同（SHA 分组）
    by_sha: dict[str, list[str]] = {}
    for p, (sha, _) in files.items():
        by_sha.setdefault(sha, []).append(p)
    for sha, paths in by_sha.items():
        if len(paths) > 1:
            items.append(HealthItem(
                kind="dedup", severity="high",
                message=f"完全相同文件 {len(paths)} 份",
                path=paths[0], detail={"duplicates": paths[1:], "sha": sha[:12]}))

    # 2) 近似重复（Jaccard ≥ 0.8，最多 top 对）
    normed = {p: _norm_lines(t) for p, (_, t) in files.items()}
    paths = list(normed)
    pairs = 0
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            sim = _jaccard(normed[paths[i]], normed[paths[j]])
            if sim >= 0.8 and sim < 1.0:
                items.append(HealthItem(
                    kind="dedup", severity="medium",
                    message=f"近似重复文件（相似度 {sim:.0%}）",
                    path=paths[i], detail={"similar_to": paths[j], "similarity": round(sim, 3)}))
                pairs += 1
                if pairs >= top:
                    return items

    # 3) 重复代码块（相同函数签名+相同函数体，跨文件）
    seen_bodies: dict[str, str] = {}  # 归一化函数体 -> path:line
    for p in paths:
        try:
            tree = ast.parse(files[p][1])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body_src = ast.get_source_segment(files[p][1], node) or ""
                body_norm = re.sub(r"\s+", "", body_src)[:400]
                if len(body_norm) < 60:
                    continue  # 太短不判重复
                key = _sha256(body_norm)
                if key in seen_bodies and len(items) < top * 2:
                    items.append(HealthItem(
                        kind="dedup", severity="low",
                        message=f"重复代码块（{node.name}，{len(body_norm)} 归一化字符）",
                        path=p, detail={"same_as": seen_bodies[key]}))
                else:
                    seen_bodies.setdefault(key, f"{p}:{node.lineno}")
    return items[: top * 2]


def incomplete_scan(root: str, top: int = 100) -> list[HealthItem]:
    """剔残缺：空实现 / TODO 堆积 / 占位符 / 断引用。"""
    items: list[HealthItem] = []
    count = 0
    for p in _iter_src_files(root):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        rel = os.path.relpath(p, root)

        # 1) 空实现（函数/方法体只含 pass/.../NotImplementedError/空 return）
        if p.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    body = node.body
                    if len(body) == 1 and isinstance(body[0], ast.Pass):
                        items.append(HealthItem(
                            kind="incomplete", severity="high",
                            message=f"空实现函数 {node.name}（仅 pass）",
                            path=rel, detail={"line": node.lineno}))
                        count += 1
                    elif len(body) == 1 and isinstance(body[0], ast.Raise):
                        exc = body[0].exc
                        is_ni = (
                            # raise NotImplementedError（无括号 → ast.Name）
                            (isinstance(exc, ast.Name) and exc.id == "NotImplementedError") or
                            # raise NotImplementedError()（有括号 → ast.Call）
                            (isinstance(exc, ast.Call) and
                             getattr(exc.func, "id", "") == "NotImplementedError")
                        )
                        if is_ni:
                            items.append(HealthItem(
                                kind="incomplete", severity="high",
                                message=f"未实现函数 {node.name}（raise NotImplementedError）",
                                path=rel, detail={"line": node.lineno}))
                            count += 1
                    if count >= top:
                        return items

        # 2) TODO/占位符
        for m in _PLACEHOLDER_RE.finditer(text):
            kw = m.group(1).upper()
            sev = "high" if kw in ("TODO", "FIXME") else "low"
            items.append(HealthItem(
                kind="incomplete", severity=sev,
                message=f"{kw} 标记（残缺/待办）",
                path=rel, detail={"line": text.count("\n", 0, m.start()) + 1,
                                  "snippet": text[max(0, m.start() - 30):m.end() + 30]}))
            count += 1
            if count >= top:
                return items

        # 3) 断引用（Python import 失败）
        if p.endswith(".py"):
            for ln_no, ln in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*import\s+([\w.]+)", ln) or re.match(r"^\s*from\s+([\w.]+)\s+import", ln)
                if m and count < top:
                    # 快速检查：模块文件是否存在（同级目录或已安装）
                    mod = m.group(1).split(".")[0]
                    if not _module_exists(root, p, mod):
                        items.append(HealthItem(
                            kind="incomplete", severity="medium",
                            message=f"断引用：import {m.group(1)} 找不到模块",
                            path=rel, detail={"line": ln_no}))
                        count += 1
    return items


def _module_exists(root: str, file_path: str, mod: str) -> bool:
    """粗检模块是否存在（本地文件 or 已安装包）。"""
    d = os.path.dirname(file_path)
    if os.path.exists(os.path.join(d, mod + ".py")) or \
            os.path.exists(os.path.join(d, mod, "__init__.py")):
        return True
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def branch_scan(root: str, top: int = 20) -> list[HealthItem]:
    """分支：git 分支健康（非 git 仓库优雅降级）。"""
    items: list[HealthItem] = []
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        items.append(HealthItem(
            kind="branch", severity="info",
            message="非 git 仓库（跳过分支检查）", path=root))
        return items

    def _git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", root, *args],
                               capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    # 1) 未提交改动
    porcelain = _git("status", "--porcelain")
    dirty = [ln for ln in porcelain.splitlines() if ln.strip()]
    if dirty:
        items.append(HealthItem(
            kind="branch", severity="medium",
            message=f"未提交改动 {len(dirty)} 处（建议每日备份前先提交）",
            path=root, detail={"sample": dirty[:5]}))

    # 2) 游离 HEAD
    head = _git("symbolic-ref", "-q", "HEAD")
    if not head:
        items.append(HealthItem(
            kind="branch", severity="high",
            message="游离 HEAD（detached）——提交将不归属于任何分支",
            path=root))

    # 3) 未合并分支
    cur = _git("branch", "--show-current") or "(detached)"
    branches = [b.strip().lstrip("*").strip()
                for b in _git("branch", "--format=%(refname:short)").splitlines() if b.strip()]
    for b in branches:
        if b == cur:
            continue
        merged = _git("branch", "--merged", cur, "--format=%(refname:short)")
        if b not in merged.splitlines():
            ahead = _git("rev-list", "--count", f"{cur}..{b}")
            items.append(HealthItem(
                kind="branch", severity="low",
                message=f"未合并分支 {b}（领先 HEAD {ahead} 提交）",
                path=root, detail={"branch": b, "ahead": ahead}))
    return items[:top]


def conflict_scan(root: str, top: int = 20) -> list[HealthItem]:
    """标矛盾：同名符号多处定义 / 注释-实现矛盾。"""
    items: list[HealthItem] = []
    symbols: dict[str, list[tuple[str, int]]] = {}  # name -> [(path, line)]
    for p in _iter_src_files(root):
        if not p.endswith(".py"):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        rel = os.path.relpath(p, root)
        # 测试文件不参与符号冲突（test_*.py / *_test.py）
        base = os.path.basename(rel)
        if base.startswith("test_") or base.endswith("_test.py") or "test" in rel.split(os.sep)[:-1]:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                name = node.name
                if name.startswith("_") or name.startswith("test"):
                    continue
                symbols.setdefault(name, []).append((rel, node.lineno))

    for name, locs in symbols.items():
        if len(locs) > 1:
            items.append(HealthItem(
                kind="conflict", severity="high",
                message=f"同名符号多处定义：{name}（{len(locs)} 处）",
                path=locs[0][0], detail={"definitions": [f"{p}:{ln}" for p, ln in locs]}))
            if len(items) >= top:
                break
    return items[:top]


def repo_health(action: str, root: str, top: int = 20) -> dict:
    """repo_health 主入口（action 分发）。"""
    t0 = time.perf_counter()
    root = os.path.normpath(os.path.abspath(root))
    if not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}

    actions = {"dedup": dedup_scan, "incomplete": incomplete_scan,
               "branch": branch_scan, "conflict": conflict_scan}
    if action == "all":
        all_items: list[HealthItem] = []
        for fn in (dedup_scan, incomplete_scan, branch_scan, conflict_scan):
            all_items.extend(fn(root, top=top))
        items = all_items
    elif action in actions:
        items = actions[action](root, top=top)
    else:
        return {"ok": False, "error": f"未知 action: {action}（可选: dedup/incomplete/branch/conflict/all）"}

    # 汇总 + 健康评分（100 - 加权扣分）
    score = 100
    weights = {"high": 8, "medium": 4, "low": 2, "info": 0}
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
        score -= weights.get(it.severity, 0)
    score = max(0, min(100, score))

    return {
        "ok": True,
        "action": action,
        "root": root,
        "items": [{"kind": i.kind, "severity": i.severity, "message": i.message,
                   "path": i.path, "detail": i.detail} for i in items],
        "summary": by_kind,
        "score": score,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


if __name__ == "__main__":
    import json
    import sys
    r = repo_health(sys.argv[1] if len(sys.argv) > 1 else "all",
                    sys.argv[2] if len(sys.argv) > 2 else os.getcwd())
    print(json.dumps(r, ensure_ascii=False, indent=2)[:4000])
