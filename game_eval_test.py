# -*- coding: utf-8 -*-
"""game_eval 自测（pytest——评价系统本身不能坏，它是日常检查基准）。

用法：pytest game_eval_test.py 或 python -m pytest game_eval_test.py
"""
import os
import sys

import game_eval as ge

PROJECT = ge.PROJECT


def test_mount_rules_full6_ok():
    path = os.path.join(PROJECT, "assets/modules/rebuild/corp_structure_1x1.ron")
    ok, issues = ge.check_mount_rules(path)
    assert ok, issues


def test_mount_rules_conveyor_tri3():
    path = os.path.join(PROJECT, "assets/modules/rebuild/corp_conveyor_basic.ron")
    ok, issues = ge.check_mount_rules(path)
    assert ok, issues
    # 验证确实是 3 点（设计性限制落地）
    text = open(path, encoding="utf-8").read()
    assert text.count("MountPoint(") == 3
    for face in ["North", "South", "Bottom"]:
        assert f"face: {face}" in text


def test_mount_rules_rejects_zero_points():
    # 0 挂点模块必违规（红线）
    ron = """
    ModuleDef(schema_version: 4, id: "corp.bad", name: "bad", corp: "corp",
        category: Structure, mass: 1.0, hp: 10,
        shape: Block(dims: (1, 1, 1)),
        mount_points: [], components: [], model_path: "", tags: [])
    """
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ron", delete=False,
                                     encoding="utf-8") as f:
        f.write(ron)
        path = f.name
    try:
        ok, issues = ge.check_mount_rules(path)
        assert not ok
        assert any("无挂点" in i or "0 点" in i for i in issues)
    finally:
        os.unlink(path)


def test_mount_rules_face_level_freedom():
    # 面级自定义（2026-08-19）：普通结构件 2 点合法（"游戏"工具点面导出）；
    # 用户没点的格（cell(1,0,0)）允许不连——模块级 0 点红线
    ron = """
    ModuleDef(schema_version: 4, id: "corp.beam", name: "beam", corp: "corp",
        category: Structure, mass: 1.0, hp: 10,
        shape: Block(dims: (2, 1, 1)),
        mount_points: [
            MountPoint(cell: (0, 0, 0), face: East, accepts: Any, strength: 1.0),
            MountPoint(cell: (0, 0, 0), face: Top, accepts: Any, strength: 1.0),
        ],
        components: [], model_path: "", tags: [])
    """
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ron", delete=False,
                                     encoding="utf-8") as f:
        f.write(ron)
        path = f.name
    try:
        ok, issues = ge.check_mount_rules(path)
        assert ok, issues
    finally:
        os.unlink(path)
    # 整模块 0 点仍禁止（模块级红线）
    ron0 = ron.replace(
        "mount_points: [\n            MountPoint(cell: (0, 0, 0), face: East, accepts: Any, strength: 1.0),\n            MountPoint(cell: (0, 0, 0), face: Top, accepts: Any, strength: 1.0),\n        ]",
        "mount_points: [],")
    with tempfile.NamedTemporaryFile("w", suffix=".ron", delete=False,
                                     encoding="utf-8") as f:
        f.write(ron0)
        path = f.name
    try:
        ok, issues = ge.check_mount_rules(path)
        assert not ok
        assert any("无挂点" in i or "0 点" in i for i in issues)
    finally:
        os.unlink(path)


def test_key_bindings_pass():
    ok, issues, summary = ge.check_key_bindings(PROJECT)
    assert ok, issues
    assert "BINDINGS" in summary


def test_templates_pass():
    ok, issues, summary = ge.check_templates(PROJECT)
    assert ok, issues
    assert "7 个模板" in summary


def test_full_eval_pass_and_report():
    md, data = ge.run(PROJECT, report_dir=None)
    assert data["verdict"] == "PASS"
    assert data["mount"]["ok"] == data["mount"]["total"] > 100
    assert data["keys"]["ok"]
    assert data["templates"]["ok"]
    assert "连接点规则" in md
    assert "按键覆盖" in md
    assert "程序化模板" in md


def test_generators_known_set_consistency():
    # 已知生成函数集合必须包含全部 7 个（core procgen.rs 分发表）
    assert ge.KNOWN_GENERATORS == {
        "vehicle", "rock", "tree", "terrain_tile", "building", "road", "table",
    }
