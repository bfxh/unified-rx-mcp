# -*- coding: utf-8 -*-
"""game_verify/game_rules 测试（阶段4：可复现验证 + 项目级规则 + 音频方法论）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_game_verify_godot(tmp_path, monkeypatch):
    """Godot 项目无 smoke 脚本 → 检出（可复现验证缺口）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("[application]\n", encoding="utf-8")
    d = json.loads(server._call("game", {"action": "verify", "path": str(repo)})[0].text)
    assert d["ok"] is False, d
    assert any("smoke" in c["msg"] for c in d["checks"]), d
    # 补齐 smoke 后通过
    tools = repo / "tools"
    tools.mkdir()
    (tools / "smoke.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (repo / "logs").mkdir()
    d = json.loads(server._call("game", {"action": "verify", "path": str(repo)})[0].text)
    assert d["ok"] is True, d


def test_game_rules_save_load(tmp_path, monkeypatch):
    """game_rules 读写（通用默认 + 项目覆盖——在游戏文件里再搞一个）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    rules = {"engine": "bevy",
             "physics_range": {"min": 0.01, "max": 100.0},
             "interaction_rules": ["esc_main_menu", "pickup_preview"]}
    d = json.loads(server._call("game", {"action": "rules", "path": str(repo), "sub_action": "save",
                                               "rules": rules})[0].text)
    assert d["ok"] is True and d["path"].endswith("game_rules.json"), d
    d = json.loads(server._call("game", {"action": "rules", "path": str(repo)})[0].text)
    assert d["ok"] is True and d["rules"]["engine"] == "bevy", d
    # 未创建 → load 诚实报缺
    repo2 = tmp_path / "empty"
    repo2.mkdir()
    d = json.loads(server._call("game", {"action": "rules", "path": str(repo2)})[0].text)
    assert d["ok"] is False and "无 game_rules.json" in d["error"], d


def test_game_rules_physics_override(tmp_path, monkeypatch):
    """game_rules 的 physics_range 覆盖默认红线范围（项目可调）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "phys.rs").write_text(
        "fn setup() {\n    let wheel_radius: f32 = 50.0;\n}\n", encoding="utf-8")
    # 默认范围（1e-3..1e4）：50 不报
    d = json.loads(server._call("game", {"action": "check", "path": str(repo)})[0].text)
    assert not any(i["rule"] == "physics_scale" for i in d["issues"]), d
    # 项目范围（0.01..1.0）：50 超限报
    server._call("game", {"action": "rules", "path": str(repo), "sub_action": "save",
                           "rules": {"physics_range": {"min": 0.01,
                                                       "max": 1.0}}})
    d = json.loads(server._call("game", {"action": "check", "path": str(repo)})[0].text)
    assert any(i["rule"] == "physics_scale" for i in d["issues"]), d
    assert d.get("game_rules"), "应附 game_rules 信息"
