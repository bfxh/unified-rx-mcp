# -*- coding: utf-8 -*-
"""S55：ide_edit 直接单测（registry 层已有 test_s34/test_ide_fix，本文件钉单位语义）。

真实断言对象：occ 语义 / CRLF 保留 / dry_run 不落盘 / 0 匹配结构化失败 / 定位与上下文。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools  # noqa: E402,F401
from tools.ide_edit import code_context, ide_edit_multi, ide_rename, locate_edit


# ---------- ide_edit_multi ----------

def test_edit_occ_second_occurrence(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("TARGET\nmid\nTARGET\n", encoding="utf-8", newline="")
    r = ide_edit_multi(file_path=str(f), edits=[
        {"old_lines": ["TARGET"], "new_lines": ["Y"], "occ": 2}])
    assert r["applied"] == 1
    assert f.read_text(encoding="utf-8") == "TARGET\nmid\nY\n"


def test_edit_preserves_crlf(tmp_path):
    f = tmp_path / "b.py"
    f.write_text("alpha\r\nbeta\r\n", encoding="utf-8", newline="")
    r = ide_edit_multi(file_path=str(f), edits=[
        {"old_lines": ["beta"], "new_lines": ["BETA"]}])
    assert r["applied"] == 1 and r["eol"] == "CRLF"
    raw = f.read_bytes()
    assert b"alpha\r\nBETA\r\n" == raw


def test_edit_dry_run_no_write(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("one\ntwo\n", encoding="utf-8", newline="")
    before = f.read_bytes()
    r = ide_edit_multi(file_path=str(f), edits=[
        {"old_lines": ["two"], "new_lines": ["TWO"]}], dry_run=True)
    assert r["applied"] == 1 and r["dry_run"] is True
    assert "+TWO" in r["diff"] and f.read_bytes() == before


def test_edit_zero_match_structural_failure(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("keep\n", encoding="utf-8", newline="")
    r = ide_edit_multi(file_path=str(f), edits=[
        {"old_lines": ["nope"], "new_lines": ["x"]}])
    assert r["error"].startswith("0 应用") and r["applied"] == 0
    assert f.read_text(encoding="utf-8") == "keep\n"


# ---------- locate_edit / code_context / ide_rename ----------

def test_locate_edit_hits_and_empty_query(tmp_path):
    f = tmp_path / "e.py"
    f.write_text("hello_world = 1\nother\nhello_world + 1\n", encoding="utf-8")
    r = locate_edit(path=str(tmp_path), query="hello_world")
    assert r["total"] >= 1 and r["references_in_scan"] == 2
    assert all("hello_world" in h["snippet"] for h in r["hits"])
    bad = locate_edit(path=str(tmp_path), query="   ")
    assert bad["error"] and "query 为空" in bad["error"]


def test_code_context_window(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n",
                 encoding="utf-8")
    # radius 有下限 5（工具语义：radius<5 按 5 处理）
    r = code_context(path=str(f), cursor_line=5, radius=5)
    assert r["start"] == 1 and r["end"] == 9
    assert "line1" in r["content"] and "line9" in r["content"]
    r2 = code_context(path=str(f), cursor_line=5, radius=2)
    assert (r2["start"], r2["end"]) == (r["start"], r["end"])  # 下限生效
    assert r2["lang"] == "python"


def test_ide_rename_plan(tmp_path):
    for name in ("one.py", "sub/two.py"):
        p = tmp_path / name
        p.parent.mkdir(exist_ok=True)
        p.write_text("foo = 1\n", encoding="utf-8")
    r = ide_rename(root=str(tmp_path), symbol="foo", new_name="bar")
    assert r["files_affected"] == 2 and r["total_occurrences"] == 2
    assert r["plan"] is None
    r2 = ide_rename(root=str(tmp_path), symbol="foo", new_name="bar",
                    include_plan=True)
    assert len(r2["plan"]) == 2
