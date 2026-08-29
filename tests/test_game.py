# -*- coding: utf-8 -*-
"""S55：game 域测试（此前零测试）。

game_check 走真实文件扫描；blender_verify 用替身 subprocess 固定两种环境
（Blender 在/不在），不依赖桌面真实状态。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools  # noqa: E402,F401
import tools.game as game_mod  # noqa: E402
from tools.game import blender_verify, game_check  # noqa: E402


def _fake_run(results):
    """按调用顺序返回预设 CompletedProcess。"""
    it = iter(results)

    def fake(*a, **kw):
        return next(it)
    return fake


# ---------- game_check ----------

def test_game_check_keys_and_magic_numbers(tmp_path):
    g = tmp_path / "scripts"
    g.mkdir()
    (g / "player.gd").write_text(
        "var speed = 766\n"
        "func _p():\n    if Input.is_key_pressed(KEY_W) or KeyCode::KeyA:\n"
        "        move(speed)\n", encoding="utf-8")
    (g / "readme.txt").write_text("not scanned\n", encoding="utf-8")
    r = game_check(path=str(g))
    assert r["files"] == 1
    assert any(kb["key"] == "KeyA" for kb in r["key_bindings"])
    assert any(f["rule"] == "magic_number" and f["value"] == "766"
               for f in r["findings"])
    assert "键位绑定" in r["summary"]


def test_game_check_single_file_and_errors(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text("fn a() { let y = 1234; }\n", encoding="utf-8")
    r = game_check(path=str(f))
    assert r["files"] == 1
    bad = game_check(path=str(tmp_path / "ghost"))
    assert "error" in bad and "路径不存在" in bad["error"]


# ---------- blender_verify（替身 subprocess，确定性） ----------

def test_blender_verify_not_running(monkeypatch):
    monkeypatch.setattr(game_mod.subprocess, "run",
                        _fake_run([subprocess.CompletedProcess([], 0,
                                   stdout="no blender here\n")]))
    r = blender_verify()
    assert r["ok"] is False and "未运行" in r["note"]


def test_blender_verify_running_screenshot(monkeypatch, tmp_path):
    shot = str(tmp_path / "s.png")
    monkeypatch.setattr(game_mod.subprocess, "run", _fake_run([
        subprocess.CompletedProcess([], 0,
                                    stdout="blender.exe   1234 Console\n"),
        subprocess.CompletedProcess([], 0, stdout=""),
    ]))
    r = blender_verify(screenshot_path=shot)
    assert r["ok"] is True and r["screenshot"] == shot
    assert r["blender_processes"] and r["ocr_enabled"] is False
