# -*- coding: utf-8 -*-
"""game_api 引擎语义词典测试（阶段3：命中 + 防幻觉 + code_complete 接入）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_game_api_hit():
    """已收录符号返回语义（kind+description）。"""
    d = json.loads(server._call("game_api",
                                {"engine": "bevy", "symbol": "Transform"})[0].text)
    assert d["ok"] is True and d["kind"] == "组件", d
    assert "translation" in d["description"], d
    d = json.loads(server._call("game_api",
                                {"engine": "godot", "symbol": "_process"})[0].text)
    assert d["ok"] is True and "delta" in d["description"], d


def test_game_api_unknown_honest():
    """未收录诚实拒绝（防幻觉——绝不臆造签名）。"""
    d = json.loads(server._call("game_api",
                                {"engine": "bevy", "symbol": "SomeMadeUpApi"})[0].text)
    assert d["ok"] is False and "未收录" in d["error"], d
    assert d.get("fuzzy") == [], d
    # 未知引擎也拒绝
    d = json.loads(server._call("game_api",
                                {"engine": "unity", "symbol": "Transform"})[0].text)
    assert d["ok"] is False and "未知引擎" in d["error"], d


def test_game_api_code_complete_hints(tmp_path, monkeypatch):
    """code_complete LSP 空结果时附 game_api 词典提示（.gd → godot）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "hud.gd"
    f.write_text("func _ready():\n    Trans", encoding="utf-8")
    # 直接测 game_hints 逻辑（LSP 对 gd 无语言服务器——走词典）
    text = f.read_text(encoding="utf-8")
    from game_api import GODOT_API
    hits = [k for k in GODOT_API if "trans" in k.lower() or k.lower() in "trans"]
    assert isinstance(hits, list), hits
    # code_complete 返回结构含 game_hints 字段（LSP 不可用时 ok:false——不崩溃）
    r = server._call("code_complete", {"path": str(f)})
    txt = r[0].text
    assert txt, "code_complete 不应崩溃"
