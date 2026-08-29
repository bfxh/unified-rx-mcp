# -*- coding: utf-8 -*-
"""S35：cargo clippy lint + LSP 诊断进修复轮。"""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
import swe_repair  # noqa: E402

CARGO = shutil.which("cargo")
CLIPPY = shutil.which("cargo-clippy")


@pytest.mark.skipif(CARGO is None or CLIPPY is None, reason="no cargo/clippy")
def test_ide_build_clippy_warns(tmp_path):
    proj = tmp_path / "lintme"
    subprocess.run([CARGO, "init", "--name", "lintme", str(proj)], check=True,
                   capture_output=True)
    (proj / "src" / "main.rs").write_text(
        "fn main() {\n    let x = true;\n    if x == true {\n        println!(\"a\");\n    }\n}\n",
        encoding="utf-8")
    r = call_tool("ide_build", {"path": str(proj), "action": "lint"})
    assert r["tool"] == "clippy"
    assert r["warnings"], "x == true 应触发 clippy 警告"
    assert any("equality checks against true" in w["msg"] for w in r["warnings"])


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


def test_diag_section_format(tmp_path, monkeypatch):
    # S46：统一诊断 error+warning 都回喂（clippy warning 走同一通道）
    def fake(name, args):
        if name == "ide_diagnostics":
            return {"ok": True, "result": {"diagnostics": [
                {"source": "pylsp", "file": "a.py", "line": 7,
                 "severity": "error", "message": "undefined name 'z'"},
                {"source": "clippy", "file": "b.rs", "line": 3,
                 "severity": "warning", "message": "equality checks"},
            ]}}
        return {"ok": True, "result": {}}
    monkeypatch.setattr(swe_repair.registry, "call", fake)
    txt = swe_repair._diag_section(str(tmp_path), ["a.py", "b.rs"])
    assert "[DIAGNOSTICS" in txt and "[pylsp]" in txt and "a.py:7" in txt
    assert "WARN" in txt and "clippy" in txt and "equality checks" in txt
    assert "ERROR [pylsp]" in txt
    monkeypatch.setattr(swe_repair.registry, "call",
                        lambda n, a: {"ok": True, "result": {"diagnostics": []}})
    assert swe_repair._diag_section(str(tmp_path), ["a.py"]) == ""


def test_repair_prompt_contains_lsp_section(tmp_path, monkeypatch):
    # 修复轮提示词必须带 LSP 段（fake 全链路，零 API）
    import json
    import subprocess
    sv = swe_repair.sv
    iid = "x__y-1"
    # 仓库：a.txt 可应用
    root = tmp_path / iid
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text("y = 2\n", encoding="utf-8")
    for c in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                              "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                        "-c", "user.name=t"] + c, check=True)
    patch = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
             "@@ -1 +1 @@\n-y = 2\n+y = 42\n")
    monkeypatch.setattr(sv, "WORK", str(tmp_path))
    monkeypatch.setattr(sv, "_uv_py", lambda p: sys.executable)
    monkeypatch.setattr(swe_repair.swe_p3, "load_sample",
                        lambda: [{"instance_id": iid, "repo": "pallets/flask",
                                  "test_patch": patch, "ftb": ["t"], "ptb": [],
                                  "issue": "i", "gold_patch": ""}])
    state = {"runs": 0}

    def fake_tests(*a, **k):
        # 1=基线 fail，2=候选 round0 fail，3=修复后 pass
        state["runs"] += 1
        return (0, "") if state["runs"] >= 3 else (1, "FAILED x::test_a\nE   assert 1 == 2")
    monkeypatch.setattr(sv, "_run_tests", fake_tests)
    monkeypatch.setattr(swe_repair.registry, "call",
                        lambda n, a: ({"ok": True, "result": {"diagnostics": [
                            {"source": "pylsp", "file": "a.py", "line": 0,
                             "severity": "error", "message": "undefined name 'z'"}]}}
                            if n == "ide_diagnostics" else {"ok": True, "result": {}}))
    good = {"choices": [{"message": {
        "content": "```sr\npath: a.txt\n<<<<<<< SEARCH\ny = 2\n=======\ny = 42\n"
                   ">>>>>>> REPLACE\n```", "tool_calls": None}}]}
    captured = {}

    def fake_chat(ch, model, msgs, tools_schema=None):
        captured["msgs"] = list(msgs)
        return good
    monkeypatch.setattr(swe_repair.AB, "chat", fake_chat)
    monkeypatch.setattr(swe_repair.AB, "load_channel", lambda ch: object())
    monkeypatch.setattr(swe_repair.AB, "usage_of", lambda r: (10, 10))
    # 结果文件：candidate 为空 → fresh 路径产出 sr 块（good）
    fp = os.path.join(sv.RESULTS_DIR, f"{iid}_A.json")
    old = None
    if os.path.exists(fp):
        old = open(fp, encoding="utf-8").read()
        os.remove(fp)
    rec = {"instance_id": iid, "arm": "A", "mech": {"candidate_diff": ""},
           "answer": ""}
    json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        args = type("A", (), {"channel": "c", "model": "m", "max_repairs": 1,
                              "force": True, "ids": "",
                              "variant": "signals"})()
        swe_repair.repair_loop(args)
    finally:
        if old is None:
            os.remove(fp)
        else:
            open(fp, "w", encoding="utf-8").write(old)
    joined = "\n".join(str(mm.get("content"))[:300] for mm in captured.get("msgs", []))
    assert "[DIAGNOSTICS" in joined
    assert "undefined name 'z'" in joined
