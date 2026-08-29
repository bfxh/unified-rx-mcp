# -*- coding: utf-8 -*-
"""S42 推广：守卫硬化回归（能力探针 / infra 故障检测 / skip 语义）。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
import swe_verify as sv  # noqa: E402
import swe_repair  # noqa: E402


def call_tool(name, args):
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


# ---------- _is_infra_failure ----------

def test_infra_signatures_cn_en():
    assert sv._is_infra_failure("行 3: : 未找到命令") == "未找到命令"
    assert sv._is_infra_failure("x: command not found") == "command not found"
    assert sv._is_infra_failure("No module named pytest") == "No module named pytest"


def test_infra_not_triggered_by_node_drift_or_real_fail():
    # pytest node id 漂移（ERROR: not found）≠ 基础设施故障
    assert sv._is_infra_failure("ERROR: not found: /mnt/c/x/test_plot.py") is None
    # 真实测试失败 ≠ 基础设施故障
    assert sv._is_infra_failure("E   assert 1 == 2\nFAILED x::t") is None
    assert sv._is_infra_failure("") is None


# ---------- _venv_py_ok 能力探针（带缓存） ----------

def test_venv_py_ok_caches_and_detects(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    py = str(tmp_path / "fake_python.exe")
    assert sv._venv_py_ok(py) is True
    assert sv._venv_py_ok(py) is True
    assert len(calls) == 1                     # 缓存命中，不重复探测

    import swe_verify
    swe_verify._VENV_PY_PROBE[py] = False
    assert sv._venv_py_ok(py) is False


# ---------- verify_one：infra → skip（区别于测试失败） ----------

def test_verify_one_infra_is_skip_not_fail(tmp_path, monkeypatch):
    import subprocess
    root = tmp_path / "x__y-1"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("v = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    patch = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
             "@@ -1 +1,2 @@\n v = 1\n+v = 2\n")
    monkeypatch.setattr(sv, "WORK", str(tmp_path))
    monkeypatch.setattr(sv, "_run_tests",
                        lambda *a, **k: (None, "infra: No module named pytest"))
    rec = {"instance_id": "x__y-1", "mech": {"candidate_diff": ""}}
    inst = {"test_patch": patch, "ftb": ["t"], "ptb": [], "repo": "x/y"}
    v = sv.verify_one(rec, inst, "py", {})
    assert v.get("skip", "").startswith("infra")
    assert v.get("verified") is None           # 不记假测试失败


# ---------- repair_loop：base 阶段 infra → skip ----------

def test_repair_base_infra_is_skip(tmp_path, monkeypatch):
    import subprocess
    sv = swe_repair.sv
    iid = "x__y-1"
    root = tmp_path / iid
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("v = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    patch = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
             "@@ -1 +1,2 @@\n v = 1\n+v = 2\n")
    monkeypatch.setattr(sv, "WORK", str(tmp_path))
    monkeypatch.setattr(sv, "_uv_py", lambda p: sys.executable)
    monkeypatch.setattr(sv, "_venv_py_ok", lambda p: True)
    monkeypatch.setattr(sv, "_run_tests",
                        lambda *a, **k: (None, "infra: No module named pytest"))
    monkeypatch.setattr(swe_repair.AB, "load_channel", lambda ch: object())
    monkeypatch.setattr(swe_repair.AB, "chat",
                        lambda ch, m, msgs, tools_schema=None: {
                            "choices": [{"message": {"content": "", "tool_calls": None}}]})
    monkeypatch.setattr(swe_repair.AB, "usage_of", lambda r: (10, 10))
    monkeypatch.setattr(swe_repair.swe_p3, "load_sample",
                        lambda: [{"instance_id": iid, "repo": "pallets/flask",
                                  "test_patch": patch, "ftb": ["t"], "ptb": [],
                                  "issue": "i", "gold_patch": ""}])
    fp = os.path.join(sv.RESULTS_DIR, f"{iid}_A.json")
    old = open(fp, encoding="utf-8").read() if os.path.exists(fp) else None
    if old is not None:
        os.remove(fp)
    rec = {"instance_id": iid, "arm": "A", "mech": {"candidate_diff": ""},
           "answer": ""}
    json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        args = type("A", (), {"channel": "c", "model": "m", "max_repairs": 1,
                              "force": True, "ids": "",
                              "variant": "signals"})()
        swe_repair.repair_loop(args)
        d = json.load(open(fp, encoding="utf-8"))
        rp = d.get("repair_signals") or {}
        assert rp.get("skip", "").startswith("infra")
        assert rp.get("verified") is None
    finally:
        if old is None:
            os.remove(fp)
        else:
            open(fp, "w", encoding="utf-8").write(old)
