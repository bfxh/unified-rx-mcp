# -*- coding: utf-8 -*-
"""tests/test_bevy.py —— Bevy 专项规则测试（用户：引擎重点优化 Bevy）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_bevy_ui_dead_button(tmp_path):
    """Bevy 死按钮：Button+Marker spawn 后无任何 Query 交互处理才算死。"""
    f = Path(tmp_path) / "ui.rs"
    # 真死按钮：无 marker 处理器
    f.write_text(
        "fn build(mut commands: Commands) {\n"
        "    commands.spawn((Button, OrphanBtn, Node { ..default() }));\n"
        "}\n", encoding="utf-8")
    r = registry.call("ui_check", {"path": str(tmp_path)})
    assert r["ok"]
    engines = {i.get("engine") for i in r["result"]["issues"]}
    msgs = " | ".join(i.get("msg", "") for i in r["result"]["issues"])
    assert "bevy" in engines and "OrphanBtn" in msgs, f"应检出 bevy 死按钮: {r['result']}"

    # 误报防线：Marker 在跨 system 被 Query<(&M, &Interaction)> 处理 → 不算死
    f2 = Path(tmp_path / "_ok")
    f2.mkdir()
    (f2 / "ui.rs").write_text(
        "fn build(mut commands: Commands) {\n"
        "    commands.spawn((Button, GoodBtn, Node { ..default() }));\n"
        "}\n"
        "fn click(mut q: Query<(&GoodBtn, &Interaction), Changed<Interaction>>) {}\n",
        encoding="utf-8")
    r2 = registry.call("ui_check", {"path": str(f2)})
    dead = [i for i in r2["result"]["issues"] if "GoodBtn" in i.get("msg", "")]
    assert not dead, f"有处理器的按钮不得误报: {dead}"


def test_bevy_old_system(tmp_path):
    """Bevy 旧 API：add_system 检出（S4-D1 分级后为 info 线索 + kind=clue）。"""
    f = Path(tmp_path) / "main.rs"
    f.write_text(
        "fn main() {\n"
        "    App::new().add_system(update);\n"
        "}\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    issues = [i for i in r["result"]["issues"] if i["rule"] == "bevy_old_system"]
    assert issues, f"add_system 应检出: {r['result']}"
    assert issues[0]["severity"] == "info" and issues[0].get("kind") == "clue"


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


def test_bevy_phys_rules(tmp_path):
    """avian3d 物理规则（09-05）：LockedAxes 魔数 / Static+Velocity / 手写支撑力。"""
    f = Path(tmp_path) / "vehicle.rs"
    f.write_text(
        "fn spawn(mut c: Commands) {\n"
        "    c.spawn((RigidBody::Static, LinearVelocity::new(2.0, 0.0, 0.0)));\n"
        "    let bits = LockedAxes::from_bits(0b000_101);\n"
        "}\n"
        "fn susp(mut f: Query<&mut ExternalForce>) {\n"
        "    f.apply_force_at_point(Vec3::Y * spring_force, point);\n"
        "}\n",
        encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    rules = {i["rule"] for i in r["result"]["issues"]}
    for want in ("bevy_phys_locked_axes_bits", "bevy_phys_static_with_velocity",
                 "bevy_phys_manual_support_force"):
        assert want in rules, f"{want} 应检出: {rules}"
    sev = {i["rule"]: i.get("severity") for i in r["result"]["issues"]}
    assert sev["bevy_phys_static_with_velocity"] == "low"
    assert sev["bevy_phys_manual_support_force"] == "med"


def test_bevy_phys_static_velocity_fp_guard(tmp_path):
    """误报防线：普通 Static/Dynamic、测试 fixture 的 ::ZERO、matches! 运行时判断
    （VoxelForge 09-05 甄别的三类 FP）不得触发 static_with_velocity。"""
    f = Path(tmp_path) / "vehicle.rs"
    f.write_text(
        "fn ground(mut c: Commands) {\n"
        "    c.spawn((RigidBody::Static, Collider::cuboid(1.0, 1.0, 1.0)));\n"
        "    c.spawn((RigidBody::Dynamic, Mass(2.0)));\n"
        "}\n"
        "fn fixture(mut c: Commands) {\n"
        "    c.spawn((RigidBody::Static, LinearVelocity::ZERO, AngularVelocity::ZERO));\n"
        "}\n"
        "fn runtime_judge(rb: Option<&RigidBody>) -> bool {\n"
        "    let v = Vec3::ZERO;\n"
        "    rb.is_some_and(|b| matches!(*b, RigidBody::Static)) && v == LinearVelocity::ZERO.0\n"
        "}\n"
        "fn cross_statement(mut c: Commands) {\n"
        "    // S74：上一条 Dynamic spawn 的速度逗号 + 200 字符内另一条 Static spawn，\n"
        "    // 不得跨语句误连（static_with_velocity 第三分支须锚同一 spawn 元组）\n"
        "    c.spawn((LinearVelocity::new(1.0, 0.0, 0.0), RigidBody::Dynamic, Collider::cuboid(1.0, 1.0, 1.0)));\n"
        "    c.spawn((RigidBody::Static, Collider::cuboid(1.0, 1.0, 1.0)));\n"
        "}\n",
        encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    hits = [i for i in r["result"]["issues"] if i["rule"] == "bevy_phys_static_with_velocity"]
    assert not hits, f"三类已知 FP 不得命中: {hits}"
