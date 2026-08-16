# -*- coding: utf-8 -*-
"""game_check 引擎中立性测试（2026-08-14，skill M1/M2/M5 落地验证）。

12 用例：3 引擎（Bevy/Godot/Unity）× 3 规则（frame_io/input_unthrottled/
physics_scale）+ 3 寄存器（character/abstract/serious）——同一样例在三种
引擎写法都命中（"failure mode is rule-level rather than API-level"）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

CASES = [
    ("bevy.rs",
     "fn update(mut q: Query<&mut Transform>, asset: Res<AssetServer>) {\n"
     "    let h = asset.load(\"models/car.glb\");\n}\n", "frame_io"),
    ("godot.gd",
     "func _process(delta):\n"
     "    var f = FileAccess.open(\"res://data.txt\", FileAccess.READ)\n"
     "    pass\n", "frame_io"),
    ("unity.cs",
     "void Update() {\n"
     "    var s = System.IO.File.ReadAllText(\"data.txt\");\n}\n", "frame_io"),
    ("bevy2.rs",
     "fn player_input(mut q: Query<&mut Transform>) {\n"
     "    if keys.just_pressed(KeyCode::Space) { jump(); }\n}\n",
     "input_unthrottled"),
    ("godot2.gd",
     "func _input(event):\n"
     "    if event.is_action_pressed(\"jump\"):\n        jump()\n",
     "input_unthrottled"),
    ("unity2.cs",
     "void Update() {\n"
     "    if (Input.GetKeyDown(KeyCode.Space)) { Jump(); }\n}\n",
     "input_unthrottled"),
    ("phys.rs",
     "fn setup() {\n    let wheel_radius: f32 = 50000.0;\n}\n",
     "physics_scale"),
    ("phys2.gd",
     "func _ready():\n    spring_stiffness = 1e6\n", "physics_scale"),
    ("phys3.cs",
     "void Awake() {\n    this.mass = 0.00001f;\n}\n", "physics_scale"),
    ("char.rs",
     "pub struct Slime {}\npub fn pet_the_kitten() {}\n", "character"),
    ("abst.rs",
     "fn spawn_shard(drone: &Node) {}\nfn grid_pulse() {}\n", "abstract"),
    ("ser.rs",
     "fn survive_scavenger(grim: &World) {}\n", "serious"),
]


def _run_case(tmp_path, fn, src, want):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    p = repo / fn
    p.write_text(src, encoding="utf-8")
    if want in ("character", "abstract", "serious"):
        d = json.loads(server._call("game", {"action": "feel", "path": str(p)})[0].text)
        return d.get("register") == want, d
    d = json.loads(server._call("game", {"action": "check", "path": str(repo)})[0].text)
    got = {i.get("rule") for i in d.get("issues", [])}
    return want in got, d


def test_game_check_engine_neutral(tmp_path, monkeypatch):
    """3 引擎 × 3 规则 + 3 寄存器全部命中（引擎无关性契约）。"""
    # tmp_path 在系统临时目录（沙盒外）——测试扩展沙盒根
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    for fn, src, want in CASES:
        ok, d = _run_case(tmp_path, fn, src, want)
        assert ok, f"{fn}: 期望 {want} 未命中: {d.get('issues', [])[:3] or d}"


def test_game_check_physics_scale_excludes_normal():
    """正常数量级不误报（1e-3..1e4 内）。"""
    import game_check
    src = "fn setup() {\n    let wheel_radius: f32 = 0.35;\n"
    src += "    let gravity: f32 = 9.81;\n}\n"
    issues = game_check.check_game_invariants(src, "x.rs")
    phys = [i for i in issues if i["rule"] == "physics_scale"]
    assert not phys, f"正常参数不应报: {phys}"


def test_game_feel_unknown_register_honest():
    """无信号时诚实返回 unknown（防幻觉——skill 原则不臆测）。"""
    import game_check
    r = game_check.judge_register("fn compute(x: f32) -> f32 { x }\n", "x.rs")
    assert r["register"] == "unknown"
    assert "不确定时选更克制" in r["advice"]
