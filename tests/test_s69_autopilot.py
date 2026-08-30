# -*- coding: utf-8 -*-
"""S69：开发目录自动驾驶回归钉。

- discover_projects 只认带项目标记的一层目录
- autopilot 同步跑 → 快照 done + verdict 排序
- 去重窗口：新鮮快照复用不重跑；force 重跑
- 启动失败项目落 error 不静默
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import ide_autopilot as ap  # noqa: E402


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def test_discover_projects_markers(tmp_path):
    a = tmp_path / "proj_a"
    a.mkdir()
    (a / ".git").mkdir()
    b = tmp_path / "proj_b"
    b.mkdir()
    (b / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (tmp_path / "not_proj").mkdir()
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")
    found = ap.discover_projects(str(tmp_path))
    assert len(found) == 2, found
    assert all("proj_" in p for p in found)


def test_autopilot_sync_snapshot(tmp_path):
    p1 = tmp_path / "clean_proj"
    p1.mkdir()
    (p1 / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(p1)
    p2 = tmp_path / "bad_proj"
    p2.mkdir()
    (p2 / "evil.py").write_text(
        "def f(u):\n    return eval(u)\n", encoding="utf-8")
    _git_init(p2)
    snap = ap.autopilot_run(root=str(tmp_path), force=True, sync=True,
                            vscode=False)
    assert snap["status"] == "done"
    assert len(snap["projects"]) == 2
    assert snap["projects"][0]["verdict"] == "issues"   # issues 排最前
    assert snap["projects"][1]["verdict"] in ("clean", "warn")


def test_autopilot_dedupe_window(tmp_path):
    p1 = tmp_path / "solo"
    p1.mkdir()
    (p1 / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(p1)
    s1 = ap.autopilot_run(root=str(tmp_path), force=True, sync=True,
                          vscode=False)
    t1 = s1["finished"]
    s2 = ap.autopilot_run(root=str(tmp_path), sync=True, vscode=False)
    assert s2.get("reused") is True
    assert s2["finished"] == t1                          # 复用未重跑
    s3 = ap.autopilot_run(root=str(tmp_path), force=True, sync=True,
                          vscode=False)
    assert s3.get("reused") is False and s3["finished"] >= t1


def test_auto_report_tool(tmp_path):
    p1 = tmp_path / "proj"
    p1.mkdir()
    (p1 / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(p1)
    r = registry.call("ide_auto_report", {"root": str(tmp_path), "force": True,
                                          "sync": True, "vscode": False})
    assert r["ok"] or r.get("status") == "done"
    res = r.get("result") or r
    assert res["status"] == "done" and res["projects"]


def test_startup_failure_is_recorded_not_silent(tmp_path):
    """体检异常（如项目半损坏）必须落 error 快照，不许静默死。"""
    p1 = tmp_path / "broken"
    p1.mkdir()
    (p1 / ".git").mkdir()          # 有标记但不是有效仓库
    snap = ap.autopilot_run(root=str(tmp_path), force=True, sync=True,
                            vscode=False)
    assert snap["status"] in ("done", "error")
    assert "error" in snap or snap["projects"]
