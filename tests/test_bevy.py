# -*- coding: utf-8 -*-
"""tests/test_bevy.py —— Bevy 专项规则测试（用户：引擎重点优化 Bevy）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_bevy_ui_dead_button(tmp_path):
    """Bevy 死按钮：Button 无交互处理。"""
    f = Path(tmp_path) / "ui.rs"
    f.write_text(
        "fn build(mut commands: Commands) {\n"
        "    commands.spawn((Button, Node { ..default() }));\n"
        "}\n", encoding="utf-8")
    r = registry.call("ui_check", {"path": str(tmp_path)})
    assert r["ok"]
    engines = {i.get("engine") for i in r["result"]["issues"]}
    assert "bevy" in engines, f"应检出 bevy 引擎: {r['result']}"


def test_bevy_old_system(tmp_path):
    """Bevy 旧 API：add_system 检出。"""
    f = Path(tmp_path) / "main.rs"
    f.write_text(
        "fn main() {\n"
        "    App::new().add_system(update);\n"
        "}\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    rules = {i["rule"]: i.get("severity") for i in r["result"]["issues"]}
    assert rules.get("bevy_old_system") == "medium", f"add_system 应检出: {rules}"


def test_bevy_text_old(tmp_path):
    """Bevy 旧式 TextBundle。"""
    f = Path(tmp_path) / "ui.rs"
    f.write_text("fn ui(mut commands: Commands) {\n    commands.spawn(TextBundle { ..default() });\n}\n",
                 encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    rules = {i["rule"] for i in r["result"]["issues"]}
    assert "bevy_text_old" in rules, f"TextBundle 应检出: {rules}"


def test_bevy_query_single(tmp_path):
    """Bevy query.single() 风险。"""
    f = Path(tmp_path) / "sys.rs"
    f.write_text("fn sys(q: Query<&T>) { let t = q.single(); }\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    rules = {i["rule"]: i.get("severity") for i in r["result"]["issues"]}
    assert rules.get("bevy_query_single") == "low", f"query.single 应检出: {rules}"
