"""test_ide_session.py — IDE R5 编辑会话模型测试（IDE_ENHANCE_PLAN R5）。

覆盖：
  1. FastLineIndex：行号↔偏移互转（含空文本/越界/多行）
  2. PieceTable：insert/delete/replace + 相邻合并 + 编辑后文本正确
  3. 大文件场景：5000 行文本定位（性能冒烟——不应逐行慢扫）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ide_session import FastLineIndex, PieceTable  # noqa: E402


# ── FastLineIndex ──
def test_fli_basic():
    idx = FastLineIndex("ab\ncd\nef\n")
    assert idx.line_count == 4
    assert idx.line_start(1) == 3
    assert idx.position_to_offset(1, 1) == 4  # 'd'
    assert idx.offset_to_position(4) == (1, 1)
    assert idx.offset_to_position(0) == (0, 0)
    assert idx.offset_to_position(8) == (2, 2)  # "ef\n" 的 "ef" 末尾
    assert idx.offset_to_position(9) == (3, 0)  # 末尾（换行后）


def test_fli_empty():
    idx = FastLineIndex("")
    assert idx.line_count == 1
    assert idx.position_to_offset(0, 0) == 0


def test_fli_roundtrip():
    text = "\n".join(f"line {i}" for i in range(100))
    idx = FastLineIndex(text)
    for line in range(0, 100, 7):
        col = line % 4  # "line N" 行长 ≥ 5，col 保证行内
        off = idx.position_to_offset(line, col)
        l, c = idx.offset_to_position(off)
        assert (l, c) == (line, col)


# ── PieceTable ──
def test_pt_insert():
    pt = PieceTable("hello world")
    pt.insert(5, ",")
    assert pt.text() == "hello, world"


def test_pt_delete():
    pt = PieceTable("hello world")
    pt.delete(5, 11)
    assert pt.text() == "hello"


def test_pt_replace():
    pt = PieceTable("hello world")
    pt.replace(6, 11, "there")
    assert pt.text() == "hello there"


def test_pt_multiple_edits():
    pt = PieceTable("abcdef")
    pt.insert(3, "X")      # abcXdef
    pt.delete(0, 1)        # bcXdef
    pt.replace(2, 4, "YZ")  # 删 "Xd"（offset 2-4）→ bcef；插 YZ → bcYZef
    assert pt.text() == "bcYZef"


def test_pt_merge_adjacent():
    pt = PieceTable("abc")
    pt.insert(1, "12")   # a12bc
    pt.insert(3, "3")    # "12" 末尾（offset 3）→ 同源相邻合并
    assert pt.text() == "a123bc"
    assert pt.edit_count() == 2


def test_pt_line_index_after_edit():
    pt = PieceTable("line1\nline2\n")
    pt.insert(0, "L0\n")
    idx = pt.line_index()
    assert idx.line_count == 4
    assert pt.text().startswith("L0\n")


def test_pt_large_file_perf():
    """5000 行大文件：编辑 + 行定位（性能冒烟）。"""
    big = "\n".join(f"fn f{i}() {{}}" for i in range(5000))
    pt = PieceTable(big)
    pt.replace(0, 5, "// comment")
    idx = pt.line_index()
    assert idx.line_count == 5000
    assert idx.position_to_offset(4999, 0) >= 0
