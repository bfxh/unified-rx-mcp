# -*- coding: utf-8 -*-
"""predict_impact 测试（阶段2：改前预测——影响面+教训+规则提示）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_predict_impact_affected_files(tmp_path, monkeypatch):
    """改公共函数 → 预测全部调用方文件（影响面）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text(
        "pub fn compute_area(w: f32) -> f32 { w }\n", encoding="utf-8")
    (repo / "main.rs").write_text(
        "fn main() {\n    let a = compute_area(1.0);\n}\n", encoding="utf-8")
    d = json.loads(server._call("predict_impact", {
        "root": str(repo), "symbol": "compute_area"})[0].text)
    assert d["ok"] is True, d
    affected = d["predict"]["affected_files"]
    files = [f.replace("\\", "/").split("/")[-1] for f, _ in affected]
    assert "main.rs" in files, f"调用方应被预测: {files}"
    assert d["predict"]["reference_count"] == 1, d
    assert d["risk"] in ("low", "medium", "high"), d


def test_predict_impact_rule_hints(tmp_path, monkeypatch):
    """目标文件含规则风险 → 预测附规则提示（unwrap/无限循环）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.rs").write_text(
        "pub fn risky() {\n    let x = foo().unwrap();\n}\n", encoding="utf-8")
    d = json.loads(server._call("predict_impact", {
        "root": str(repo), "symbol": "risky",
        "file_hint": str(repo / "a.rs")})[0].text)
    assert d["ok"] is True, d
    hints = " ".join(d["rule_hints"])
    assert "unwrap" in hints, f"应有 unwrap 规则提示: {d['rule_hints']}"


def test_predict_impact_unknown_symbol(tmp_path, monkeypatch):
    """未知符号诚实返回（不影响面不臆测）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    d = json.loads(server._call("predict_impact", {
        "root": str(repo), "symbol": "nosuch_symbol_xyz"})[0].text)
    assert d["ok"] is False, d
    assert "未找到" in d.get("error", ""), d
