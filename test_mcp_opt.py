"""test_mcp_opt.py — MCP 优化（M1-M6）测试（2026-08-13）。

覆盖：
  M1: bug_hunt/ide_context preset 存在且步数正确
  M2: ide_context preset 链完整
  M3: cmd_cheatsheet 命令手册 + local_run 白名单执行
  M4: skill_fetch 申请制（request→list→approve→安装；reject 拒绝）
  M5: design_note 三分（settled/adjustable/doubts）
  M6: scan_trend 趋势分析
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


# ── M1/M2: preset ──
def test_bug_hunt_preset():
    steps = server._PIPELINE_PRESETS.get("bug_hunt", [])
    assert len(steps) == 4, f"bug_hunt 应 4 步: {len(steps)}"
    tools = [s["tool"] for s in steps]
    assert "rust_scan" in tools and "ide_fusion" in tools


def test_ide_context_preset():
    steps = server._PIPELINE_PRESETS.get("ide_context", [])
    assert len(steps) == 4
    assert steps[0]["tool"] == "cb_index"


# ── M3: 命令内建 ──
def test_cmd_cheatsheet():
    r = server._call("cmd_cheatsheet", {"domain": "cargo"})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    names = [c["name"] for c in d["commands"]]
    assert "test" in names and "clippy" in names


def test_cmd_cheatsheet_all():
    r = server._call("cmd_cheatsheet", {})
    d = json.loads(r[0].text)
    assert "cargo" in d["domains"] and "blender" in d["domains"]


def test_local_run_whitelist():
    """白名单：未知命令拒绝；合法命令执行。"""
    r = server._call("local_run", {"domain": "cargo", "name": "nope"})
    d = json.loads(r[0].text)
    assert d["ok"] is False and "未知命令" in d.get("error", "")


def test_local_run_echo():
    r = server._call("local_run", {"domain": "git", "name": "status"})
    d = json.loads(r[0].text)
    assert d["ok"] is True or d.get("exit") is not None  # git 可用


# ── M4: skill_fetch 申请制 ──
def test_skill_fetch_request_and_approve(tmp_path, monkeypatch):
    import skill_fetch
    monkeypatch.setattr(skill_fetch, "_APPROVAL_DIR", str(tmp_path / "approvals"))
    skills_dir = str(tmp_path / "skills")
    r = server._call("skill_fetch", {"action": "request",
                                     "task": "给集团做 Blender 建模 需要 blender-modeling",
                                     "skills_dir": skills_dir})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert len(d["approvals"]) >= 1
    aid = d["approvals"][0]["id"]

    # list 有 pending
    r2 = server._call("skill_fetch", {"action": "list", "skills_dir": skills_dir})
    d2 = json.loads(r2[0].text)
    assert any(a["id"] == aid and a["status"] == "pending" for a in d2["pending"])

    # 批准 → 安装
    r3 = server._call("skill_fetch", {"action": "approve", "id": aid,
                                      "approved": True, "skills_dir": skills_dir})
    d3 = json.loads(r3[0].text)
    assert d3["ok"] is True and d3["status"] == "installed"
    installed = os.path.join(skills_dir, "blender-modeling", "SKILL.md")
    assert os.path.isfile(installed), "批准后应安装 SKILL.md"


def test_skill_fetch_reject(tmp_path, monkeypatch):
    import skill_fetch
    monkeypatch.setattr(skill_fetch, "_APPROVAL_DIR", str(tmp_path / "approvals2"))
    skills_dir = str(tmp_path / "skills2")
    r = server._call("skill_fetch", {"action": "request",
                                     "task": "rust 安全模式 rust-safety",
                                     "skills_dir": skills_dir})
    d = json.loads(r[0].text)
    aid = d["approvals"][0]["id"]
    r2 = server._call("skill_fetch", {"action": "approve", "id": aid,
                                      "approved": False, "skills_dir": skills_dir})
    d2 = json.loads(r2[0].text)
    assert d2["status"] == "rejected"
    assert not os.path.isdir(os.path.join(skills_dir, "rust-safety")), "拒绝不应安装"


# ── M5: design_note 三分 ──
def test_design_note(tmp_path):
    root = str(tmp_path)
    r = server._call("design_note", {"action": "add", "root": root,
                                     "kind": "settled", "text": "七幕剧情骨架"})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    r2 = server._call("design_note", {"action": "add", "root": root,
                                      "kind": "doubts", "text": "lua 钩子边界"})
    assert json.loads(r2[0].text)["ok"] is True
    r3 = server._call("design_note", {"action": "list", "root": root})
    d3 = json.loads(r3[0].text)
    assert d3["counts"]["settled"] == 1
    assert d3["counts"]["doubts"] == 1
    # 非法 kind 拒绝
    r4 = server._call("design_note", {"action": "add", "root": root,
                                      "kind": "bogus", "text": "x"})
    assert json.loads(r4[0].text)["ok"] is False


# ── M6: scan_trend ──
def test_scan_trend():
    r = server._call("scan_trend", {"window_days": 7})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert "tool_frequency" in d and "rule_hits" in d
