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


def test_quest_abort_and_note(tmp_path, monkeypatch):
    """IDE 增强（2026-08-13）：abort 放弃 + note 备注 + status 时长/备注计数。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q4", "任务4", "/r")
    # 备注
    r = q.add_note("发现：根因在 removal.rs 碎片归集")
    assert r["ok"] and r["notes_count"] == 1
    assert q.status()["notes_count"] == 1
    # 空备注拒绝
    assert q.add_note("   ")["ok"] is False
    # 状态含 elapsed_s
    st = q.status()
    assert "elapsed_s" in st and st["elapsed_s"] >= 0
    assert st["aborted"] is False
    # abort：标记放弃、保留状态可复盘
    r = q.abort()
    assert r["ok"] and r["aborted"] is True
    assert q.status()["aborted"] is True
    # 二次 abort 拒绝
    assert q.abort()["ok"] is False
    # 断点续跑后仍可见 aborted + notes
    q2 = ide_quest.resume_quest("q4")
    assert q2 is not None and q2.status()["aborted"] is True
    assert q2.status()["notes_count"] == 1


def test_quest_tool_abort_note_integration(tmp_path, monkeypatch):
    """ide_quest 工具 abort/note action 端到端。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    r = server._call("ide_quest", {"action": "new", "quest_id": "qi4",
                                    "task": "任务", "repo": "/x"})
    assert json.loads(r[0].text)["ok"]
    r = server._call("ide_quest", {"action": "note", "quest_id": "qi4",
                                    "text": "上下文备注"})
    d = json.loads(r[0].text)
    assert d["ok"] and d["notes_count"] == 1
    r = server._call("ide_quest", {"action": "abort", "quest_id": "qi4"})
    assert json.loads(r[0].text)["ok"] is True
    r = server._call("ide_quest", {"action": "status", "quest_id": "qi4"})
    assert json.loads(r[0].text)["status"]["aborted"] is True


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


def test_ide_fusion_impact_via_references(tmp_path):
    """IDE 增强四：双引擎影响面校验（tree 侧引用来自 ide_references）。"""
    repo = tmp_path
    (repo / "lib.rs").write_text(
        "pub fn compute_area(w: f32) -> f32 { w }\n"
        "fn main() {\n"
        "    let a = compute_area(2.0);\n"
        "    let b = compute_area(3.0);\n"
        "}\n",
        encoding="utf-8",
    )
    r = server._call("ide_fusion", {"path": str(repo), "action": "impact",
                                    "symbol": "compute_area"})
    d = json.loads(r[0].text)
    assert d["ok"], d
    assert d["definition_count"] == 1
    assert d["reference_count"] == 2, f"两个调用应计入: {d['reference_count']}"
    assert len(d["tree_refs"]) == 1, f"引用文件应只有 lib.rs: {d['tree_refs']}"
    assert "verdict" in d and "source" in d
    # 带 LSP 引用对比：一致 → verdict 一致
    r2 = server._call("ide_fusion", {"path": str(repo), "action": "impact",
                                     "symbol": "compute_area",
                                     "lsp_refs": [str(repo / "lib.rs")]})
    d2 = json.loads(r2[0].text)
    assert d2["verdict"] == "一致", f"LSP 与 tree 引用一致应判定一致: {d2['verdict']}"
