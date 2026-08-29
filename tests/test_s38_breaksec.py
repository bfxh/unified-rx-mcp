# -*- coding: utf-8 -*-
"""S38：java/go 断点后端注入防护 + rust 诚实报错。"""
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


def test_java_class_injection_rejected(tmp_path):
    (tmp_path / "Boom.class").write_bytes(b"\xca\xfe\xba\xbe")
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": ["java", "-cp", ".", "Boom"],
                   "breakpoints": [{"class": "Boom; run; quit", "line": 1}]})
    assert "error" in r and "注入" in r["error"]


def test_go_func_regex_injection_rejected(tmp_path):
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    r = call_tool("ide_break",
                  {"path": str(tmp_path),
                   "cmd": ["app.exe"],
                   "breakpoints": [{"func": "main.*)(?i))|((x"}]})
    assert "error" in r and "注入" in r["error"]


def test_rust_honest_no_gdb(tmp_path):
    (tmp_path / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    r = call_tool("ide_break",
                  {"path": str(tmp_path), "cmd": ["main.exe"],
                   "breakpoints": [{"file": "main.rs", "line": 1}]})
    assert "error" in r and ("gdb" in r["error"] or "rust" in r["error"])
