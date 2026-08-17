#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_layer_check.py — 分层检查 + 写完即模拟测试。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import layer_check as lc  # noqa: E402


def test_ui_three_layers_order():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        # 只布局
        p = os.path.join(root, "layout_only.html")
        open(p, "w", encoding="utf-8").write(
            "<div style='width:100px;height:50px;position:absolute;left:0;top:0'>x</div>")
        r = lc.ui(p)
        assert r["layers"]["layout"]["done"] is True
        assert r["stage"] == "animation"  # 布局完成，等动画
        # 布局+动画+美术 全做
        p2 = os.path.join(root, "full.html")
        open(p2, "w", encoding="utf-8").write(
            "<div style='width:100px;height:50px;position:absolute;left:0;top:0;"
            "transition:all 0.3s ease;animation:fade 1s;color:#ff0000;"
            "background:linear-gradient(red,blue);font-size:14px'>x</div>")
        r2 = lc.ui(p2)
        assert r2["stage"] == "全部完成"
        assert r2["violations"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ui_violation_order():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        # 有动画没布局 → 顺序违规
        p = os.path.join(root, "anim_only.html")
        open(p, "w", encoding="utf-8").write(
            "<div style='transition:all 0.3s;animation:fade 1s;color:#f00'>x</div>")
        r = lc.ui(p)
        assert r["violations"], r
        assert "layout" in r["violations"][0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_code_three_layers():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        # 骨架齐全但逻辑少
        p = os.path.join(root, "a.py")
        open(p, "w", encoding="utf-8").write(
            "def f():\n    return 1\n\ndef g():\n    return 2\n")
        r = lc.code(p)
        assert r["layers"]["skeleton"]["done"] is True
        assert r["layers"]["logic"]["done"] is True  # return 出现
        # 优化层：魔法数字
        p2 = os.path.join(root, "b.py")
        open(p2, "w", encoding="utf-8").write(
            "def h():\n    if 12345:\n        return 67890\n")
        r2 = lc.code(p2)
        assert r2["layers"]["optimize"]["done"] is False
        assert "魔法数字" in r2["layers"]["optimize"]["detail"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_simulate_python_ok():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        p = os.path.join(root, "ok.py")
        open(p, "w", encoding="utf-8").write("def f():\n    return 42\n")
        r = lc.simulate(p)
        assert r["passed"] is True
        names = [c["name"] for c in r["checks"]]
        assert "ast 语法" in names and "py_compile" in names
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_simulate_python_syntax_error():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        p = os.path.join(root, "bad.py")
        open(p, "w", encoding="utf-8").write("def f(:\n    pass\n")
        r = lc.simulate(p)
        assert r["passed"] is False
        assert "语法" in r["checks"][0]["name"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_simulate_import_failure():
    root = tempfile.mkdtemp(prefix="lc_")
    try:
        p = os.path.join(root, "imp.py")
        open(p, "w", encoding="utf-8").write(
            "import definitely_not_a_real_module_xyz\nx = 1\n")
        r = lc.simulate(p)
        assert r["passed"] is False
        assert any(not c["ok"] for c in r["checks"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_layer_check_unknown_action():
    r = lc.layer_check("nope", __file__)
    assert r["ok"] is False


# ── 2026-08-17：剪辑/3D 动画分层模板 ────────────────────────────────────

def test_clip_three_layers():
    root = tempfile.mkdtemp(prefix="lc_clip_")
    try:
        # 只粗剪（素材+顺序）
        p = os.path.join(root, "剪辑计划.md")
        open(p, "w", encoding="utf-8").write(
            "素材: shot_01.mp4, shot_02.mp4\n顺序: 01 02 03\ntimeline 时间线\n")
        r = lc.layer_check("clip", p)
        assert r["layers"]["raw"]["done"] is True
        assert r["stage"] == "fine"  # 等精剪
        # 三层全做
        p2 = os.path.join(root, "完成.md")
        open(p2, "w", encoding="utf-8").write(
            "素材: shot_01.mp4, shot_02.mp4, shot_03.mp4\n顺序: 01 02 03\ntimeline 时间线\n"
            "transition crossfade fade 转场\npacing 节奏 marker\n"
            "color grade lut 调色\naudio sound 音频 音效\n字幕 subtitle\n")
        r2 = lc.layer_check("clip", p2)
        assert r2["stage"] == "全部完成"
        assert r2["violations"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clip_violation_order():
    root = tempfile.mkdtemp(prefix="lc_clip_")
    try:
        # 有精剪/调色但无粗剪 → 违规
        p = os.path.join(root, "违规.md")
        open(p, "w", encoding="utf-8").write(
            "transition crossfade 转场\npacing 节奏\ncolor grade 调色\naudio 音频\n")
        r = lc.layer_check("clip", p)
        assert r["violations"], r
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_anim3d_three_layers():
    root = tempfile.mkdtemp(prefix="lc_anim_")
    try:
        # 建模绑定 + K帧，无渲染 → stage=render
        p = os.path.join(root, "动画.md")
        open(p, "w", encoding="utf-8").write(
            "建模 model mesh 网格 拓扑\n绑定 rig armature bone 骨骼\n"
            "keyframe K帧 animation action fcurve\n动画 驱动 driver\n")
        r = lc.layer_check("anim3d", p)
        assert r["layers"]["model"]["done"] is True
        assert r["layers"]["key"]["done"] is True
        assert r["stage"] == "render"  # 等渲染层
        # 渲染层补上
        open(p, "w", encoding="utf-8").write(
            "建模 model mesh 网格 拓扑\n绑定 rig armature bone 骨骼\n"
            "keyframe K帧 animation action fcurve\n动画 驱动 driver\n"
            "渲染 render cycles 材质 material 灯光 light 输出 resolution\n")
        r2 = lc.layer_check("anim3d", p)
        assert r2["stage"] == "全部完成"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_layer_check_unknown_action_media():
    r = lc.layer_check("nope", __file__)
    assert r["ok"] is False
