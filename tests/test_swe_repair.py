# -*- coding: utf-8 -*-
"""S25 swe_repair 离线回归：触碰文件提取 / sr 应用回打 / 文件块截断。"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import swe_repair as sr  # noqa: E402

DIFF = ("diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/y.py b/y.py\n"
        "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-c\n+d\n"
        "diff --git a/z.py b/z.py\n"
        "--- a/z.py\n+++ b/z.py\n@@ -1 +1 @@\n-e\n+f\n")


def test_touched_files_extracts_and_caps():
    assert sr._touched_files(DIFF) == ["x.py", "y.py", "z.py"]
    assert len(sr._touched_files(DIFF + DIFF)) == 3


def test_file_block_truncates(tmp_path):
    p = tmp_path / "big.py"
    p.write_text("x" * 30000, encoding="utf-8")
    b = sr._file_block(str(tmp_path), "big.py")
    assert "middle truncated" in b and len(b) < 10000
    assert sr._file_block(str(tmp_path), "nope.py") == ""


def test_apply_and_diff_roundtrip(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    blocks = [("a.txt", "line2", "line2b")]
    applied, fails, gdiff, grounds = sr._apply_and_diff(str(tmp_path), blocks)
    assert applied == 1 and fails == [] and gdiff.strip()
    # apply_sr 还原后 gdiff 重打 → 工作树处于已修改态
    assert "line2b" in (tmp_path / "a.txt").read_text(encoding="utf-8")
    # 重复应用同一 diff 应失败（已应用）→ _applied_now 语义
    assert sr._applied_now(str(tmp_path), gdiff) is True


def test_apply_and_diff_bad_block(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("line1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    applied, fails, gdiff, _ = sr._apply_and_diff(
        str(tmp_path), [("a.txt", "no-such", "x")])
    assert applied == 0 and fails and not gdiff.strip()
