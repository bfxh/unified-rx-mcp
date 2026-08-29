# -*- coding: utf-8 -*-
"""R3：rename 落盘 + ide_impact 影响面。

fake server 协议闭环（rename 回显请求 uri）；_apply_text_edits 纯函数单测
（UTF-16 列 / CRLF / 倒序拼接 / 越界钳制）；授权语义（不落盘拒绝）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import lsp as lsp_mod  # noqa: E402
from tools.lsp import _apply_text_edits  # noqa: E402


def _fake_env(tmp_path, monkeypatch):
    stub = os.path.join(HERE, "fixtures", "fake_lsp_server.py")
    monkeypatch.setenv("UNIFIED_RX_LSP_CMD_PYTHON", f"{sys.executable} {stub}")
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    f = tmp_path / "proj"
    f.mkdir()
    m = f / "m.py"
    m.write_text("alpha_beta()\n", encoding="utf-8")
    return str(m), lsp_mod


def _stop_sessions(mod):
    for k in list(mod._SESSIONS):
        mod._SESSIONS[k][0].stop()


# ---------- _apply_text_edits 纯函数 ----------

def test_apply_edits_basic_and_multiline():
    src = "aa\nbb\ncc\n"
    edits = [{"range": {"start": {"line": 1, "character": 0},
                        "end": {"line": 1, "character": 2}},
              "newText": "BB"}]
    out, n = _apply_text_edits(src, edits)
    assert n == 1 and out == "aa\nBB\ncc\n"


def test_apply_edits_utf16_and_crlf():
    # 只有 astral 字符（emoji U+1F389）才占 2 个 UTF-16 单元；CJK 在 BMP 里是 1
    src = "x = '🎉' + tail\r\ny = 1\r\n"
    assert lsp_mod._from_utf16_col("x = '日本語' + tail", 15) == 15   # BMP：单元==字符
    assert lsp_mod._from_utf16_col("x = '🎉' + tail", 11) == 10      # emoji 2 单元
    edits = [{"range": {"start": {"line": 0, "character": 11},
                        "end": {"line": 0, "character": 15}},
              "newText": "END"}]
    out, n = _apply_text_edits(src, edits)
    assert n == 1
    assert out.startswith("x = '🎉' + END\r\n")
    assert "\r\n" in out                       # CRLF 保留


def test_apply_edits_out_of_range_clamped():
    src = "hi\n"
    edits = [{"range": {"start": {"line": 9, "character": 0},
                        "end": {"line": 9, "character": 2}},
              "newText": "X"}]
    out, n = _apply_text_edits(src, edits)
    assert n == 1 and out.endswith("X")        # 钳到文档尾，不丢编辑


# ---------- rename_apply 协议闭环 ----------

def test_rename_apply_requires_auth(tmp_path, monkeypatch):
    m, mod = _fake_env(tmp_path, monkeypatch)
    try:
        r = registry.call_with_context(
            "ide_lsp", {"action": "rename_apply", "file": m, "line": 0,
                        "col": 0, "new_name": "renamed_x"}, request_id="t")
        assert not r["ok"] and "授权" in r["error"]
    finally:
        _stop_sessions(mod)


def test_rename_apply_writes_files(tmp_path, monkeypatch):
    m, mod = _fake_env(tmp_path, monkeypatch)
    try:
        r = registry.call_with_context(
            "ide_lsp", {"action": "rename_apply", "file": m, "line": 0,
                        "col": 0, "new_name": "renamed_x",
                        "__authorized": True}, request_id="t")
        assert r["ok"], r.get("error")
        res = r["result"]
        assert res["applied"] is True and res["total"] == 1
        assert res["files"][0]["edits"] == 1
        # fake server 编辑 (0,0)-(0,5)：'alpha' → 'renamed_x'
        assert open(m, encoding="utf-8").read() == "renamed_x_beta()\n"
    finally:
        _stop_sessions(mod)


# ---------- ide_impact ----------

def test_impact_aggregates_and_flags_tests(tmp_path, monkeypatch):
    m, mod = _fake_env(tmp_path, monkeypatch)
    try:
        r = registry.call_with_context(
            "ide_impact", {"file": m, "line": 0, "col": 0}, request_id="t")
        assert r["ok"], r.get("error")
        res = r["result"]
        # fake server references → 2 处命中（file:///tmp/whatever.rs，不存在 → 无测试）
        assert res["total_refs"] == 2
        assert res["files"][0]["refs"] == 2
        assert res["files"][0]["has_test"] is False
        assert res["untested"]
    finally:
        _stop_sessions(mod)


def test_impact_has_test_proxy(tmp_path, monkeypatch):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "mod_x.py").write_text("x = 1\n", encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_mod_x.py").write_text("import mod_x\n",
                                                 encoding="utf-8")
    assert lsp_mod._has_local_test(str(src / "mod_x.py")) is True
    (src / "mod_y.py").write_text("y = 1\n", encoding="utf-8")
    assert lsp_mod._has_local_test(str(src / "mod_y.py")) is False
