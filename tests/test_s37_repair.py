# -*- coding: utf-8 -*-
"""S37：断点命中回喂修复轮 + ide_break 模块模式 + changed_lines。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
import swe_repair  # noqa: E402

PY = sys.executable


def test_changed_lines_parses_hunks():
    diff = ("diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -10,2 +10,5 @@\n x\n y\n+z\n"
            "diff --git a/lib/b.rs b/lib/b.rs\n"
            "--- a/lib/b.rs\n+++ b/lib/b.rs\n"
            "@@ -3 +3 @@\n-old\n+new\n")
    out = swe_repair._changed_lines(diff)
    assert out["app.py"] == [(10, 14)]
    assert out["lib/b.rs"] == [(3, 3)]


def test_break_section_formats_locals():
    hits = [{"bp_line": 10, "locals": {"speed": "7", "i": "3"},
             "stack": [{"file": "app.py", "line": 10, "fn": "drive"}]}]
    txt = swe_repair._break_section(hits)
    assert "[BREAKPOINT HITS" in txt and "speed=7" in txt and "drive" in txt
    assert swe_repair._break_section([]) == ""


def test_break_hits_via_registry(tmp_path, monkeypatch):
    seen = {}

    def fake(name, args):
        seen[name] = args
        return {"ok": True, "result": {"hits": [{"bp_line": 10,
                                                 "locals": {"speed": "7"},
                                                 "stack": [{"file": "a.py",
                                                            "line": 10,
                                                            "fn": "drive"}]}],
                                        "total": 1}}
    monkeypatch.setattr(swe_repair.registry, "call", fake)
    hits = swe_repair._break_hits(str(tmp_path), "py.exe",
                                  {"app.py": [(10, 12)]},
                                  ["tests/test_a.py::test_1"])
    assert hits and hits[0]["locals"]["speed"] == "7"
    assert seen["ide_break"]["cmd"][1:3] == ["-m", "pytest"]
    assert "tests/test_a.py::test_1" in seen["ide_break"]["cmd"]


def test_break_hits_empty_on_no_bps(monkeypatch):
    monkeypatch.setattr(swe_repair.registry, "call",
                        lambda n, a: (_ for _ in ()).throw(AssertionError))
    assert swe_repair._break_hits(".", "py", {}, []) == []


# ---------- ide_break -m 模式（pytest 在 settrace 下跑） ----------

def test_ide_break_module_mode(tmp_path):
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    v = 41\n"          # 断点行 3（v 已赋值）
        "    assert v + 1 == 42\n", encoding="utf-8")
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": [PY, "-m", "pytest", "test_ok.py", "-q"],
                   "breakpoints": [{"file": "test_ok.py", "line": 3}],
                   "max_hits": 5})
    assert r["lang"] == "python"
    assert r["total"] >= 1
    locs = r["hits"][0]["locals"]
    assert locs.get("v") == "41"


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res
