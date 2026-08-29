# -*- coding: utf-8 -*-
"""S34：ide_edit_multi dry_run 预览 / ide_build 诊断缓存 / pytest 失败解析。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools.ide import _parse_pytest  # noqa: E402

import swe_repair  # noqa: E402


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


def _w(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    return str(tmp_path)


# ---------- ide_edit_multi dry_run ----------

def test_edit_dry_run_no_write(tmp_path):
    _w(tmp_path)
    p = str(tmp_path / "a.py")
    r = call_tool("ide_edit_multi",
                  {"file_path": p, "dry_run": True, "__authorized": True,
                   "edits": [{"old_lines": ["y = 2"], "new_lines": ["y = 42"]}]})
    assert r["dry_run"] is True and r["applied"] == 1
    assert "+y = 42" in r["diff"] and "-y = 2" in r["diff"]
    assert "y = 42" not in (tmp_path / "a.py").read_text(encoding="utf-8")


def test_edit_dry_run_then_real(tmp_path):
    _w(tmp_path)
    p = str(tmp_path / "a.py")
    args = {"file_path": p, "__authorized": True,
            "edits": [{"old_lines": ["y = 2"], "new_lines": ["y = 42"]}]}
    assert call_tool("ide_edit_multi", {**args, "dry_run": True})["applied"] == 1
    r = call_tool("ide_edit_multi", args)
    assert r.get("dry_run") is not True and r["applied"] == 1
    assert "y = 42" in (tmp_path / "a.py").read_text(encoding="utf-8")


def test_edit_dry_run_mismatch_reports(tmp_path):
    _w(tmp_path)
    p = str(tmp_path / "a.py")
    r = call_tool("ide_edit_multi",
                  {"file_path": p, "dry_run": True, "__authorized": True,
                   "edits": [{"old_lines": ["nope"], "new_lines": ["z"]}]})
    assert r["applied"] == 0 and "0 应用" in r["error"]


# ---------- ide_build 诊断缓存 ----------

def test_build_cache_hit_and_invalidate(tmp_path):
    p = _w(tmp_path)
    r1 = call_tool("ide_build", {"path": p})
    assert r1.get("cached") is not True
    r2 = call_tool("ide_build", {"path": p})
    assert r2.get("cached") is True and r2["ok"] is True
    # 改源文件 → 指纹失效 → 重跑
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    r3 = call_tool("ide_build", {"path": p})
    assert r3.get("cached") is not True and r3["ok"] is False


# ---------- pytest 失败解析 ----------

def test_parse_pytest_failed_and_asserts():
    txt = ("============================= test session starts ===\n"
           "FAILED tests/test_x.py::test_alpha - assert 1 == 2\n"
           "FAILED tests/test_x.py::test_beta\n"
           "E   assert 1 == 2\n"
           "E    +  where 1 = <something>\n"
           "=== 2 failed in 0.5s ===\n")
    failed, asserts = _parse_pytest(txt)
    assert "tests/test_x.py::test_alpha" in failed
    assert "tests/test_x.py::test_beta" in failed
    assert any("assert 1 == 2" in a for a in asserts)


def test_structured_frames_pytest(tmp_path=None):
    txt = ("FAILED tests/test_x.py::test_alpha - assert 1 == 2\n"
           "E   assert 1 == 2\n")
    txt_out = swe_repair._structured_frames(txt)
    assert "[STRUCTURED · pytest]" in txt_out
    assert "FAILED tests/test_x.py::test_alpha" in txt_out
