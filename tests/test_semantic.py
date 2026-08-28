# -*- coding: utf-8 -*-
"""S31 code_semantic：向量空间语义检索回归。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401


def _make_tree(tmp_path):
    (tmp_path / "clock.rs").write_text(
        "pub struct Clock {\n    pub elapsed: f32,  // 经过的时间\n}\n\n"
        "impl Clock {\n    // 时钟累加\n    pub fn tick(&mut self, dt: f32) {\n"
        "        self.elapsed += dt;\n    }\n}\n\n"
        "pub fn reset_clock(c: &mut Clock) {\n    c.elapsed = 0.0;\n}\n",
        encoding="utf-8")
    (tmp_path / "drive.rs").write_text(
        "// 旋转载具角度\npub fn rotate_vehicle_y(angle: f32) {\n"
        "    let rad = angle.to_radians();\n    let _ = rad;\n}\n\n"
        "pub fn drive_forward(speed: f32) {\n    let _ = speed;\n}\n",
        encoding="utf-8")
    (tmp_path / "ui.py").write_text(
        "def render_panel(stats):\n    \"\"\"画载具面板。\"\"\"\n"
        "    return stats\n\n\nclass PanelCache:\n    def __init__(self):\n"
        "        self.items = []\n", encoding="utf-8")


def test_search_semantic_finds_without_exact_keyword(tmp_path):
    _make_tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "时钟经过的时间累加", "root": str(tmp_path),
                       "k": 5})["result"]
    syms = [h["symbol"] for h in r["hits"]]
    assert any("tick" in s or "Clock" in s or "elapsed" in s for s in syms), syms


def test_search_semantic_partial_name(tmp_path):
    _make_tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "rotat vehicle", "root": str(tmp_path),
                       "k": 5})["result"]
    syms = [h["symbol"] for h in r["hits"]]
    assert "rotate_vehicle_y" in syms


def test_related_neighbours(tmp_path):
    _make_tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "tick", "root": str(tmp_path), "mode": "related",
                       "k": 5})["result"]
    assert r["anchor"] == "tick"
    syms = [h["symbol"] for h in r["hits"]]
    assert any("Clock" in s or "elapsed" in s or "reset_clock" in s for s in syms)


def test_search_no_match_returns_empty(tmp_path):
    _make_tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "zzz qq wwww", "root": str(tmp_path),
                       "k": 5})["result"]
    assert r["total"] == 0


def test_big_input_smoke(tmp_path):
    _make_tree(tmp_path)
    big = "词" * 50000
    r = registry.call("code_semantic",
                      {"query": big, "root": str(tmp_path),
                       "k": 3})["result"]
    assert "hits" in r
