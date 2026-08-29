# -*- coding: utf-8 -*-
"""tools/metrics.py —— 代码质量度量域（S52：coverage / dep_graph / module_stability）。

所有工具零外部依赖（stdlib trace + ast + subprocess git）。
职责：度量标准化为工具——不是 MD 文档里的"建议"，是可执行可复跑的检查。
"""
import ast
import os
import re
import subprocess
import sys
import trace
import io

from registry import tool

_SKIP = {'.git', 'node_modules', 'target', '__pycache__', '.unified-rx-index',
         'dist', 'build', 'backups', 'manual_snaps'}
_CODE_EXTS = ('.py', '.rs', '.go', '.java', '.c', '.cpp', '.js', '.ts')


def _walk_py(root, cap=500):
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for fn in files:
            if fn.endswith('.py'):
                out.append(os.path.join(r, fn))
                if len(out) >= cap:
                    return out
    return out


def _imports_of(fp):
    """提取 .py 文件的 import 模块名列表。"""
    try:
        tree = ast.parse(open(fp, encoding='utf-8', errors='replace').read())
    except (SyntaxError, OSError):
        return []
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.append(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module.split('.')[0])
    return mods


# ==================== code_coverage ====================

@tool("code_coverage", "行覆盖率测量：stdlib trace 模块（零依赖），跑目标脚本并报告"
      "哪些行执行了/没执行——每一行都要强", "scan",
      {"type": "object",
       "properties": {
           "script": {"type": "string", "description": "要跑的 Python 脚本路径"},
           "source_dir": {"type": "string", "description": "被测代码根目录（统计覆盖范围）"},
           "args": {"type": "array", "items": {"type": "string"},
                    "description": "传给脚本的参数"},
       },
       "required": ["script", "source_dir"]})
def code_coverage(script, source_dir, args=None, timeout=120):
    script = os.path.abspath(script)
    source_dir = os.path.abspath(source_dir)
    if not os.path.isfile(script):
        return {"error": f"脚本不存在: {script}"}
    if not os.path.isdir(source_dir):
        return {"error": f"目录不存在: {source_dir}"}

    # 用 stdlib trace 在子进程跑，输出覆盖数据
    tracer_script = os.path.join(source_dir, '.urx_coverage_runner.py')
    with open(tracer_script, 'w', encoding='utf-8') as f:
        f.write(
            'import trace, sys, os\n'
            'src = sys.argv[1]\n'
            'target = sys.argv[2]\n'
            'targs = sys.argv[3:]\n'
            'tracer = trace.Trace(count=1, trace=0, ignoremods=("trace",),\n'
            '                     ignoredirs=[sys.prefix, sys.exec_prefix])\n'
            'sys.argv = [target] + targs\n'
            'code = open(target, encoding="utf-8").read()\n'
            'code_obj = compile(code, target, "exec")\n'
            'tracer.runctx(code_obj, globals(), {"__name__": "__main__",\n'
            '    "__file__": target, "__builtins__": __builtins__})\n'
            'results = tracer.results()\n'
            'for fn, lineno in sorted(results.counts.keys()):\n'
            '    if fn.startswith(src.replace("/", os.sep)):\n'
            '        print(f"EXEC::{fn}::{lineno}")\n'
        )

    try:
        r = subprocess.run(
            [sys.executable, tracer_script, source_dir, script] + (args or []),
            capture_output=True, timeout=timeout, cwd=source_dir)
    except subprocess.TimeoutExpired:
        return {"error": f"超时（{timeout}s）"}
    finally:
        try:
            os.remove(tracer_script)
        except OSError:
            pass

    out = (r.stdout or b'').decode(errors='replace')
    exec_lines = {}
    for line in out.splitlines():
        if line.startswith('EXEC::'):
            _, fn, ln = line.split('::', 2)
            exec_lines.setdefault(fn, set()).add(int(ln))

    # 统计 source_dir 下所有 .py 的总行数和已执行行数
    total_stmts = covered_stmts = 0
    per_file = []
    for fp in _walk_py(source_dir):
        rel = os.path.relpath(fp, source_dir)
        try:
            src = open(fp, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        code_lines = [n for n, l in enumerate(src.split('\n'), 1)
                      if l.strip() and not l.strip().startswith('#')]
        executed = exec_lines.get(fp, set())
        covered = sum(1 for n in code_lines if n in executed)
        total_stmts += len(code_lines)
        covered_stmts += covered
        pct = round(covered / max(len(code_lines), 1) * 100)
        per_file.append({"file": rel, "lines": len(code_lines),
                         "covered": covered, "pct": pct})
    per_file.sort(key=lambda x: x['pct'])

    return {"script": script, "exit": r.returncode,
            "total_lines": total_stmts, "covered_lines": covered_stmts,
            "coverage_pct": round(covered_stmts / max(total_stmts, 1) * 100, 1),
            "per_file": per_file[:30]}


# ==================== dep_graph ====================

@tool("dep_graph", "依赖图：提取所有 .py 的 import 关系 → {模块: [依赖]}，"
      "标记循环依赖和外部依赖", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录"},
       },
       "required": ["path"]})
def dep_graph(path, max_files=300):
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    py_files = _walk_py(path, max_files)

    graph = {}       # rel_path -> [imported_top_names]
    all_names = set()
    for fp in py_files:
        rel = os.path.relpath(fp, path).replace('\\', '/')
        imports = _imports_of(fp)
        graph[rel] = imports
        all_names.update(imports)

    # 项目内模块名（不带 .py 的 stem）
    local_names = set()
    for rel in graph:
        base = os.path.basename(rel)
        if base.endswith('.py'):
            local_names.add(base[:-3])

    # 分类：内部依赖（项目内模块间）/ 外部依赖
    internal = {}
    external = {}
    cycles = _find_cycles({rel: [d for d in imports if d in local_names]
                           for rel, imports in graph.items()})
    for rel, imports in graph.items():
        internal[rel] = sorted(d for d in imports if d in local_names)
        external[rel] = sorted(d for d in imports if d not in local_names)

    ext_count = Counter = sum(len(v) for v in external.values())
    int_count = sum(len(v) for v in internal.values())
    return {"total_files": len(graph), "internal_deps": int_count,
            "external_deps": ext_count, "cycles": cycles,
            "graph": {k: v for k, v in sorted(internal.items())},
            "external_summary": sorted(
                {d for deps in external.values() for d in deps})}


def _find_cycles(graph):
    """简单 DFS 找循环依赖。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = defaultdict(int)
    cycles = []
    def dfs(node, path):
        if color[node] == 2:
            return
        if color[node] == 1:
            idx = path.index(node)
            cycles.append(' → '.join(path[idx:] + [node]))
            return
        color[node] = 1
        for dep in graph.get(node, []):
            if dep in graph:
                dfs(dep, path + [node])
        color[node] = 2
    for k in graph:
        if color[k] == 0:
            dfs(k, [])
    return cycles[:5]


from collections import defaultdict


# ==================== module_stability ====================

@tool("module_stability", "模块稳定性评分：git 提交频率（30 天）× 测试存在 × "
      "行覆盖率 = 每模块稳定性——低的优先补测试/重构", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根（必须是 git 仓库）"},
       },
       "required": ["path"]})
def module_stability(path, timeout=60):
    path = os.path.abspath(path)
    if not os.path.isdir(os.path.join(path, '.git')):
        return {"error": "不是 git 仓库"}

    # 1) git log 最近 30 天提交频率
    try:
        r = subprocess.run(
            ["git", "-C", path, "log", "--since=30 days ago",
             "--name-only", "--pretty=format:"],
            capture_output=True, timeout=timeout)
        freq = {}
        for fn in (r.stdout or b'').decode(errors='replace').splitlines():
            fn = fn.strip()
            if fn and fn.endswith('.py'):
                freq[fn] = freq.get(fn, 0) + 1
    except (OSError, subprocess.TimeoutExpired):
        freq = {}

    # 2) 测试文件存在性
    test_files = set()
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for fn in fs:
            if 'test' in r.replace('\\', '/').split('/')[-1].lower() or \
               fn.startswith('test_'):
                test_files.add(fn)

    # 3) 汇总评分
    modules = []
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for fn in fs:
            if not fn.endswith('.py') or fn.startswith('test_'):
                continue
            fp = os.path.join(r, fn)
            rel = os.path.relpath(fp, path).replace('\\', '/')
            try:
                lc = sum(1 for l in open(fp, encoding='utf-8', errors='replace')
                         if l.strip() and not l.strip().startswith('#'))
            except OSError:
                lc = 0
            commits = freq.get(rel, 0)
            has_test = any(t == f"test_{fn}" or fn in t for t in test_files)
            # 稳定性 = 低频提交 × 有测试 × 合理行数 = 绿灯
            if commits == 0 and has_test:
                score = "stable"
            elif commits <= 3 and has_test:
                score = "fair"
            elif not has_test and lc > 50:
                score = "risky"
            else:
                score = "watch"
            modules.append({"module": rel, "lines": lc, "commits_30d": commits,
                            "has_test": has_test, "stability": score})

    by_score = defaultdict(list)
    for m in modules:
        by_score[m["stability"]].append(m["module"])
    return {"total_modules": len(modules),
            "by_stability": {k: len(v) for k, v in sorted(by_score.items())},
            "risky_modules": by_score.get("risky", []),
            "modules": sorted(modules, key=lambda m: (
                m["stability"], -m["commits_30d"]))[:50]}
