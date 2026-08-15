# -*- coding: utf-8 -*-
"""replay_core 测试（阶段3：操作录制/重放——偶现变必现）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_core as rc  # noqa: E402


def test_record_and_run_tool(monkeypatch, tmp_path):
    """录制工具步骤 → 重放成功。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    r1 = rc.replay_record("seq1", {"type": "tool", "tool": "math_ops",
                                   "args": {"action": "add", "a": 1, "b": 2}})
    assert r1["ok"] is True and r1["total"] == 1
    run = rc.replay_run("seq1")
    assert run["ok"] is True
    assert run["results"][0]["result"].strip() == "3"


def test_replay_detects_failure(monkeypatch, tmp_path):
    """失败步骤 → failed_at 定位（偶现变必现）。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    rc.replay_record("seq2", {"type": "tool", "tool": "math_ops",
                              "args": {"action": "add", "a": 1, "b": 1}})
    rc.replay_record("seq2", {"type": "tool", "tool": "no_such_boom", "args": {}})
    run = rc.replay_run("seq2")
    assert run["ok"] is False
    assert run["failed_at"] == 2
    assert run["results"][1]["error"].startswith("Error:")


def test_cmd_needs_authorization(monkeypatch, tmp_path):
    """cmd 步骤默认跳过（需显式授权）。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    rc.replay_record("seq3", {"type": "cmd", "cmd": "echo hi"})
    run = rc.replay_run("seq3")
    assert run["results"][0]["skipped"]
    # 授权后执行
    rc.replay_record("seq3", {"type": "cmd", "cmd": "echo hi",
                              "authorized": True})
    run2 = rc.replay_run("seq3", stop_on_fail=False)
    last = run2["results"][-1]
    assert last["ok"] is True and last["returncode"] == 0


def test_invalid_name():
    r = rc.replay_record("../evil", {"type": "tool", "tool": "x"})
    assert r["ok"] is False


def test_missing_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    run = rc.replay_run("never_recorded")
    assert run["ok"] is False
    assert "不存在" in run["error"]


def test_replay_list(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    rc.replay_record("lst1", {"type": "tool", "tool": "math_ops", "args": {}})
    lst = rc.replay_list()
    assert lst["ok"] is True
    names = [r["name"] for r in lst["replays"]]
    assert "lst1" in names
