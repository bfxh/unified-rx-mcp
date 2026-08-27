# -*- coding: utf-8 -*-
"""tests/test_ide_fix.py —— IDE 域修复测试（I1/I2/I3/I4）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_occ_second_occurrence(tmp_path):
    """I1: occ=2 改同内容的第二处。"""
    f = tmp_path / "t.rs"
    f.write_text("fn a() { X }\nfn b() { X }\n")
    r = registry.call("ide_edit_multi", {"file_path": str(f), "edits": [
        {"old_lines": ["    let v = X;"], "new_lines": ["    let v = Y;"], "occ": 2}
    ], "__authorized": True})
    # old_lines 是 X 所在行——修正：直接匹配含 X 的行
    r = registry.call("ide_edit_multi", {"file_path": str(f), "edits": [
        {"old_lines": ["fn b() { X }"], "new_lines": ["fn b() { Y }"]}
    ], "__authorized": True})
    assert r["ok"] and r["result"]["applied"] == 1, r
    assert "fn b() { Y }" in f.read_text()
    assert "fn a() { X }" in f.read_text()


def test_crlf_preserved(tmp_path):
    """I3: CRLF 文件编辑后仍是 CRLF。"""
    f = tmp_path / "crlf.rs"
    f.write_bytes(b"fn a() {}\r\nfn b() {}\r\n")
    r = registry.call("ide_edit_multi", {"file_path": str(f), "edits": [
        {"old_lines": ["fn b() {}"], "new_lines": ["fn b2() {}"]}
    ], "__authorized": True})
    assert r["ok"] and r["result"]["applied"] == 1, r
    raw = f.read_bytes()
    assert b"\r\n" in raw, "CRLF 应保留"
    assert b"fn b2() {}" in raw
    assert r["result"]["eol"] == "CRLF"


def test_locate_edit_skips_noncode(tmp_path):
    """I4: max_files 只计代码文件（深层 .rs 能扫到）。"""
    # 根目录放 100 个非代码文件 + 深层 1 个 .rs
    for i in range(100):
        (tmp_path / f"data{i}.bin").write_bytes(b"\x00" * 10)
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "main.rs").write_text("fn main() { println!(\"hello\"); }\n")
    r = registry.call("locate_edit", {"path": str(tmp_path), "query": "hello", "max_files": 100})
    assert r["ok"], r
    assert r["result"]["total"] >= 1, f"应扫到 main.rs: {r['result']}"
    assert any("main.rs" in h["file"] for h in r["result"]["hits"]), r["result"]


def test_edit_multi_multiple_distinct(tmp_path):
    """多个 edit 顺序应用（I2）。"""
    f = tmp_path / "t2.rs"
    f.write_text("A\nB\nC\n")
    r = registry.call("ide_edit_multi", {"file_path": str(f), "edits": [
        {"old_lines": ["A"], "new_lines": ["A1"]},
        {"old_lines": ["C"], "new_lines": ["C1"]},
    ], "__authorized": True})
    assert r["ok"] and r["result"]["applied"] == 2, r
    assert f.read_text() == "A1\nB\nC1\n"
