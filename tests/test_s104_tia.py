# -*- coding: utf-8 -*-
"""S104 测试影响分析（TIA）契约：首次全量建图 → 按变更选跑 → 无变更跳过。

安全性口径（本轮核心）：**宁多跑不误跳**——新测试/无依赖记录/收集失败一律全量。
依赖图由 pytest 插件（urx_tia_plugin，audit hook）记录：收集期按文件、执行期按
测试 nodeid；`.pyc` 映射回源文件；路径统一正斜杠与 nodeid 对齐。
状态进程内保存（与 tools/cache.py 同边界）。
"""
import os
import sys

import pytest

import registry
import tools  # noqa: F401
from tools import tia as tia_mod


@pytest.fixture(autouse=True)
def _clean():
    tia_mod.reset()
    yield
    tia_mod.reset()


def _mkproj(tmp_path):
    (tmp_path / "mod_a.py").write_text("def add(a, b):\n    return a + b\n",
                                       encoding="utf-8")
    (tmp_path / "mod_b.py").write_text("def mul(a, b):\n    return a * b\n",
                                       encoding="utf-8")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_a.py").write_text("import mod_a\n\n\n"
                                 "def test_add():\n    assert mod_a.add(1, 2) == 3\n",
                                 encoding="utf-8")
    (t / "test_b.py").write_text("import mod_b\n\n\n"
                                 "def test_mul():\n    assert mod_b.mul(2, 3) == 6\n",
                                 encoding="utf-8")
    return tmp_path


def _run(proj, **extra):
    r = registry.call("ide_test", {"path": str(proj), "tia": True,
                                   "__authorized": True, **extra})
    assert r.get("ok"), r
    return r["result"]


def test_full_then_incremental_then_skip(tmp_path):
    _mkproj(tmp_path)
    r1 = _run(tmp_path)
    assert r1["tia"]["mode"] == "full-first" and r1["passed"] == 2, r1
    deps = (tia_mod.get(str(tmp_path)) or {}).get("deps") or {}
    assert deps.get("tests/test_a.py::test_add") and "mod_a.py" in \
        deps["tests/test_a.py::test_add"], deps

    (tmp_path / "mod_a.py").write_text("def add(a, b):\n    return a + b + 0\n",
                                       encoding="utf-8")
    r2 = _run(tmp_path)
    assert r2["tia"]["mode"] == "incremental", r2
    assert r2["tia"]["selected"] == 1 and r2["tia"]["skipped"] == 1, r2
    assert r2["tia"]["changed_files"] == ["mod_a.py"], r2
    assert r2["passed"] == 1, "只应执行受影响的 test_add"

    r3 = _run(tmp_path)
    assert r3["tia"]["mode"] == "skipped-no-impact", r3
    assert r3["tia"]["selected"] == 0 and r3["tia"]["skipped"] == 2, r3


def test_change_in_b_selects_b(tmp_path):
    _mkproj(tmp_path)
    _run(tmp_path)
    (tmp_path / "mod_b.py").write_text("def mul(a, b):\n    return a * b * 1\n",
                                       encoding="utf-8")
    r = _run(tmp_path)
    assert r["tia"]["mode"] == "incremental" and r["tia"]["selected"] == 1, r
    assert r["passed"] == 1, r


def test_new_test_file_always_selected(tmp_path):
    _mkproj(tmp_path)
    _run(tmp_path)
    (tmp_path / "tests" / "test_c.py").write_text(
        "def test_new():\n    assert 1 == 1\n", encoding="utf-8")
    r = _run(tmp_path)
    assert r["tia"]["mode"] == "incremental", r
    assert r["tia"]["unknown_or_new"] >= 1, r
    assert r["passed"] >= 1, r


def test_full_forces_all(tmp_path):
    _mkproj(tmp_path)
    _run(tmp_path)
    (tmp_path / "mod_a.py").write_text("def add(a, b):\n    return a + b + 0\n",
                                       encoding="utf-8")
    r = _run(tmp_path, full=True)
    assert r["tia"]["mode"] == "full-forced" and r["passed"] == 2, r


def test_non_pytest_reports_unsupported(tmp_path, monkeypatch):
    from tools import ide_test as it
    monkeypatch.setattr(it, "_detect", lambda p: ("cargo", p))
    monkeypatch.setattr(it, "_run_cargo",
                        lambda path, root, target, timeout: {
                            "tool": "cargo", "exit": 0, "passed": 1, "failed": 0,
                            "skipped": 0, "failures": []})
    r = registry.call("ide_test", {"path": str(tmp_path), "tia": True,
                                   "__authorized": True})
    assert r.get("ok"), r
    assert r["result"]["tia"]["mode"] == "unsupported", r["result"]


def test_selection_conservative_on_unknown_deps():
    sel, st = tia_mod.select(["a::t1", "a::t2"], {"a::t1": ["x.py"]}, {"y.py"})
    assert sel == ["a::t2"], "无依赖记录者必跑；依赖不相交者跳过"
    assert st["unknown_or_new"] == 1 and st["skipped"] == 1, st
