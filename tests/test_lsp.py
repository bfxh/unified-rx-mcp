# -*- coding: utf-8 -*-
"""test_lsp.py —— S17 真 LSP 客户端测试：fake server 协议闭环 + 沙盒/失败语义。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import registry  # noqa: E402
import tools     # noqa: F401,E402
from tools import lsp as lsp_mod  # noqa: E402


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    """指向 fake server 的 python LSP + tmp_path 沙盒；每测隔离会话表。"""
    stub = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "fake_lsp_server.py")
    monkeypatch.setenv("UNIFIED_RX_LSP_CMD_PYTHON", f"{sys.executable} {stub}")
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    src = tmp_path / "proj"
    src.mkdir()
    f = src / "m.py"
    f.write_text("def target_fn():\n    return 42\n\n\ntarget_fn()\n", encoding="utf-8")
    yield str(f), str(src)
    for k in list(lsp_mod._SESSIONS):
        lsp_mod._SESSIONS[k][0].stop()


def _call(action, fp=None, **kw):
    return registry.call_with_context("ide_lsp", dict({"action": action, "file": fp}, **kw),
                                      request_id="t-lsp")


def test_status_reports_python_wired(fake_env):
    r = _call("status")
    assert r["ok"] and r["result"]["servers"]["python"]["detected"] is True
    assert r["result"]["servers"]["rust"]["label"] == "rust-analyzer"


def test_definition_via_protocol(fake_env):
    fp, root = fake_env
    r = _call("definition", fp, line=0, col=5)
    assert r["ok"], r
    assert r["result"]["engine"] == "python-lsp"
    locs = r["result"]["locations"]
    assert locs and locs[0]["line"] == 7, locs          # fake 固定 Location
    assert locs[0]["file"].endswith(".rs")              # uri→path 反解


def test_references_count_and_lines(fake_env):
    fp, _ = fake_env
    r = _call("references", fp, line=0, col=5)
    refs = r["result"]["references"]
    assert r["result"]["total"] == 2 and {x["line"] for x in refs} == {7, 11}


def test_rename_plan_never_applies(fake_env):
    """rename 只出预案：applied=False 且目标文件内容不变——工具箱不抢活。"""
    fp, _ = fake_env
    before = open(fp, encoding="utf-8").read()
    r = _call("rename_plan", fp, line=0, col=5, new_name="renamed_x")
    assert r["ok"] and r["result"]["applied"] is False
    plan = r["result"]["plan"]
    assert plan and plan[0]["newText"] == "renamed_x" and plan[0]["line"] == 7
    assert open(fp, encoding="utf-8").read() == before


def test_hover_and_symbols(fake_env):
    fp, _ = fake_env
    h = _call("hover", fp, line=0, col=2)["result"]["result"]
    assert h and "fn demo" in h.get("hover", "")
    s = _call("document_symbols", fp)["result"]
    assert s["total"] == 1 and s["symbols"][0]["name"] == "demo"


def test_sandbox_rejects_outside_path(fake_env, tmp_path):
    outside = os.path.join(str(tmp_path.parent), "_out_lsp_probe.py")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    r = _call("definition", outside, line=0, col=0)
    assert not r["ok"] and "沙盒" in r["error"] or "越界" in r["error"]


def test_shutdown_kills_sessions(fake_env):
    fp, _ = fake_env
    _call("document_symbols", fp)                       # 起进程
    assert any(s.alive() for (s,) in lsp_mod._SESSIONS.values())
    r = _call("shutdown", fp)
    assert r["ok"] and r["result"]["stopped"] >= 1
