# -*- coding: utf-8 -*-
"""S71：autopilot 快照持久化（跨进程去重）+ 健康趋势工具 + ide_test 重试提示。

- 快照落 JSONL：多客户端/重启后跨进程复用（10 分钟窗口内不重跑）
- ide_health_trend：项目健康趋势——最近 N 次体检的 verdict/problems 曲线
- ide_test 失败时附 retry_hint：agent 下一步直接重跑单个失败测试
"""
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HISTORY = os.path.join(HERE, "results", "autopilot_history.jsonl")
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import ide_autopilot as ap  # noqa: E402

AUTH = {"__authorized": True}


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def _mkproj(path, body="x = 1\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text(body, encoding="utf-8")
    _git_init(path)
    return str(path)


# ---------- 快照持久化 + 跨进程去重 ----------

def test_snapshot_persisted_to_jsonl(tmp_path, monkeypatch):
    hist = tmp_path / "autopilot_history.jsonl"
    monkeypatch.setattr(ap, "HISTORY", str(hist))
    p1 = _mkproj(tmp_path / "proj")
    ap.autopilot_run(root=str(tmp_path), force=True, sync=True, vscode=False)
    assert hist.exists()
    last = json.loads(open(hist, encoding="utf-8").read().strip().split("\n")[-1])
    assert last["status"] == "done"
    assert last["root"] == str(tmp_path)
    assert any(p["path"] == p1 for p in last["projects"])


def test_cross_process_reuse(tmp_path, monkeypatch):
    """模拟重启：内存快照清空后，从磁盘历史恢复（窗口内不重跑）。"""
    hist = tmp_path / "autopilot_history.jsonl"
    monkeypatch.setattr(ap, "HISTORY", str(hist))
    p1 = _mkproj(tmp_path / "proj")
    ap.autopilot_run(root=str(tmp_path), force=True, sync=True, vscode=False)
    # 模拟进程重启：清内存快照
    ap._SNAPSHOT = {"status": "idle", "root": None, "started": None,
                    "finished": None, "projects": [], "vscode_opened": [],
                    "error": None}
    snap = ap.autopilot_run(root=str(tmp_path), sync=True, vscode=False)
    assert snap.get("reused") is True and snap["status"] == "done"
    assert any(p["path"] == p1 for p in snap["projects"])
    # 磁盘历史只有一条（没有重跑追加）
    assert len(open(hist, encoding="utf-8").read().strip().split("\n")) == 1


# ---------- 健康趋势 ----------

def test_health_trend(tmp_path, monkeypatch):
    hist = tmp_path / "autopilot_history.jsonl"
    monkeypatch.setattr(ap, "HISTORY", str(hist))
    proj = _mkproj(tmp_path / "proj")
    ap.autopilot_run(root=str(tmp_path), force=True, sync=True, vscode=False)
    # 第二次：项目内引入坏文件（verdict 恶化）
    (tmp_path / "proj" / "evil.py").write_text(
        "def f(u):\n    return eval(u)\n", encoding="utf-8")
    ap.autopilot_run(root=str(tmp_path), force=True, sync=True, vscode=False)
    r = registry.call("ide_health_trend", {"root": str(tmp_path)})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["total"] == 2
    assert res["points"][0]["verdict"] in ("clean", "warn")
    assert res["points"][-1]["verdict"] == "issues", "趋势应显示恶化"


def test_health_trend_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "HISTORY", str(tmp_path / "nope.jsonl"))
    r = registry.call("ide_health_trend", {})
    assert r["ok"] and r["result"]["total"] == 0


# ---------- ide_test 重试提示 ----------

def test_ide_test_retry_hint_on_cargo_failure(tmp_path, monkeypatch):
    out = ("test a::ok ... ok\n"
           "test a::bad ... FAILED\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")
    monkeypatch.setattr("tools.ide_test._exec",
                        lambda *a, **k: {"code": 101, "stdout": out, "stderr": ""})
    from tools.ide_test import _run_cargo
    r = _run_cargo("p", "p", None, 60)
    assert r["failed"] == 1
    assert r["retry_hint"] == "ide_test(path=..., target='a::bad')"


def test_ide_test_retry_hint_pytest(tmp_path):
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2, 'nope'\n", encoding="utf-8")
    r = registry.call("ide_test", {**AUTH, "path": str(tmp_path)})
    res = r["result"]
    assert res["failed"] == 1
    assert "test_bad.py::test_bad" in res["retry_hint"]
