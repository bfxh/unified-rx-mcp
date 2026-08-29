# -*- coding: utf-8 -*-
"""S36 ide_break：python settrace 断点记录器端到端。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


def test_break_python_records_locals_and_stack(tmp_path):
    (tmp_path / "app.py").write_text(
        "def drive(speed):\n"
        "    step = speed * 2\n"          # 断点行 3
        "    return step\n"
        "\n"
        "drive(7)\n", encoding="utf-8")
    py = sys.executable
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": [py, "app.py"],
                   "breakpoints": [{"file": "app.py", "line": 3}],
                   "max_hits": 10})
    assert r["lang"] == "python" and r["total"] >= 1, r
    h = r["hits"][0]
    assert h["bp_line"] == 3
    assert h["locals"]["speed"] == "7"
    assert any(f["fn"] == "drive" for f in h["stack"])


def test_break_python_max_hits_caps(tmp_path):
    (tmp_path / "loop.py").write_text(
        "s = 0\n"
        "for i in range(100):\n"     # 断点行 2，命中 100 次
        "    s += i\n", encoding="utf-8")
    py = sys.executable
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": [py, "loop.py"],
                   "breakpoints": [{"file": "loop.py", "line": 3}],
                   "max_hits": 5})
    assert r["total"] == 5


def test_break_rust_honest_error(tmp_path):
    (tmp_path / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    r = call_tool("ide_break",
                  {"path": str(tmp_path), "cmd": ["main.exe"],
                   "breakpoints": [{"file": "main.rs", "line": 1}]})
    assert "error" in r and "gdb" in r["error"]


def test_break_python_missing_target(tmp_path):
    py = sys.executable
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": [py, "nope.py"],
                   "breakpoints": [{"file": "nope.py", "line": 1}]})
    assert "不存在" in r["error"]
