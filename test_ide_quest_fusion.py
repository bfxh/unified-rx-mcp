"""test_ide_quest_fusion.py — IDE R6/R7 测试（IDE_ENHANCE_PLAN R6+R7）。

覆盖：
  R6：annotate_issues 符号聚合 / cross_validate_impact 双引擎校验
  R7：Quest 状态机——new→step×6→finished + 断点续跑 + list
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ide_fusion  # noqa: E402
import ide_quest  # noqa: E402
import server  # noqa: E402


# ── R6 ──
def test_annotate_issues(tmp_path):
    f = tmp_path / "a.rs"
    f.write_text("fn alpha() {\n    let x = 1 as u8;\n}\nfn beta() {\n    let y = 2 as u8;\n}\n")
    issues = [
        {"file": str(f), "line": 2, "kind": "as", "message": "as u8"},
        {"file": str(f), "line": 5, "kind": "as", "message": "as u8"},
    ]
    r = ide_fusion.annotate_issues(str(tmp_path), issues)
    assert r["total"] == 2
    assert r["symbol_map"][f"{f}#alpha"] == 1
    assert r["symbol_map"][f"{f}#beta"] == 1


def test_cross_validate_impact():
    r = ide_fusion.cross_validate_impact(
        "/repo", "f", ["a.rs", "b.rs"], ["b.rs", "c.rs"])
    assert r["lsp_only"] == ["a.rs"]
    assert r["tree_only"] == ["c.rs"]
    assert r["both"] == ["b.rs"]
    assert "差异" in r["verdict"]


# ── R7 ──
def test_quest_full_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q1", "修 UI bug", "/repo")
    assert q.current_step_name() == "diagnose"
    names = ["diagnose", "locate", "impact", "fix", "verify", "lesson"]
    for i, name in enumerate(names):
        r = q.complete_step({"evidence": f"step {name} done"})
        assert r["completed"] == name
    assert q.status()["finished"] is True
    assert q.current_step_name() == "done"


def test_quest_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q2", "任务", "/repo")
    q.complete_step({"e": 1})
    q.complete_step({"e": 2})
    # 断点续跑
    q2 = ide_quest.resume_quest("q2")
    assert q2 is not None
    assert q2.current_step_name() == "impact"
    assert q2.status()["done_steps"] == ["diagnose", "locate"]


def test_quest_list(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    ide_quest.new_quest("q3", "任务3", "/r")._save()
    lst = ide_quest.list_quests()
    assert len(lst) == 1
    assert lst[0]["quest_id"] == "q3"


def test_ide_quest_tool_integration(tmp_path, monkeypatch):
    """ide_quest 工具端到端（new → step×6 → finished）。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    r = server._call("ide_quest", {"action": "new", "quest_id": "qi1",
                                    "task": "修地形 bug", "repo": "/x"})
    d = json.loads(r[0].text)
    assert d["ok"] and d["status"]["current"] == "diagnose"
    for _ in range(6):
        r = server._call("ide_quest", {"action": "step", "quest_id": "qi1",
                                       "result": {"done": True}})
    d = json.loads(r[0].text)
    assert d.get("finished") is True
    r = server._call("ide_quest", {"action": "list"})
    d = json.loads(r[0].text)
    assert any(q["quest_id"] == "qi1" and q["finished"] for q in d["quests"])


def test_ide_fusion_tool_integration():
    r = server._call("ide_fusion", {"path": r"D:\开发\VoxelForge-Nexus"})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert d["total"] > 0
