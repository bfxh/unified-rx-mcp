# -*- coding: utf-8 -*-
"""S55：bench 一次性脚本的诚实最低保障（smoke）。

分级：
- main 有守卫的 7 个脚本 → 真导入（抓 import 破坏/重构断裂）
- import 即副作用的重脚本（vf3_battery 顶层跑全电池、_cancel_e2e_probe 顶层起
  服务器）→ 只做语法编译检查（不执行任何顶层语句）
诚实定界：这是存活检查，不是逻辑覆盖——deep 逻辑断言会冻结一次性脚本，不值。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH = os.path.join(ROOT, "bench")
sys.path.insert(0, ROOT)
sys.path.insert(0, BENCH)

_SAFE_IMPORT = ["agent_selfcheck", "h3_score", "l2_score", "p1_build",
                "p1_manual_labels", "p1_score2", "p1_snapshots", "unified_report"]
_SYNTAX_ONLY = ["vf3_battery", "_cancel_e2e_probe"]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BENCH, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_safe_bench_scripts_import():
    for name in _SAFE_IMPORT:
        mod = _load(name)
        assert mod.__name__ == name


def test_side_effect_bench_scripts_compile():
    """import 即副作用的重脚本：只编译源码（语法级），绝不执行顶层语句。"""
    for name in _SYNTAX_ONLY:
        src = open(os.path.join(BENCH, f"{name}.py"),
                   encoding="utf-8", errors="replace").read()
        compile(src, f"bench/{name}.py", "exec")


def test_unified_report_load_missing_is_none():
    mod = _load("unified_report")
    assert mod._load("no_such_file.json") is None


def test_l2_score_gate_api_exists():
    """l2_score 是 P/R 门禁计算（评测结论的事实源），API 必须在场。"""
    mod = _load("l2_score")
    assert callable(getattr(mod, "score", None)) or \
        callable(getattr(mod, "main", None)) or \
        any(callable(getattr(mod, n)) for n in dir(mod) if not n.startswith("_"))
