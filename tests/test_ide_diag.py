# -*- coding: utf-8 -*-
"""S55：ide_diag 直接单测 + S52 类错绑/缺导入回归门。

历史教训：
- S48 拆分后 ide_diag.py 用 registry.call 却没 import registry——NameError 被
  except Exception 静默吞掉，LSP+clippy 信号全空（S55 当场抓出）。
- S52 @tool 装饰器错绑到 helper——拆分脚本固有风险。
本文件把这两类事故钉死为常驻回归门。
"""
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
import tools.ide_diag as ide_diag_mod  # noqa: E402


def _fake_registry(lsp_diags=None, clippy=None, lsp_raises=False):
    """按工具名分发的 registry 替身（只替换 ide_diag 的引用，不碰真 registry）。"""
    def fake(name, args):
        if name == "ide_lsp":
            if lsp_raises:
                raise RuntimeError("lsp down")
            return {"ok": True, "result": {"diagnostics": lsp_diags or []}}
        if name == "ide_build":
            return {"ok": True, "result": clippy or {}}
        raise AssertionError(f"意外工具调用: {name}")
    return SimpleNamespace(call=fake)


# ---------- S55 核心回归：模块必须持有 registry（缺导入=信号全空的静默死亡） ----------

def test_module_has_registry_attr():
    assert hasattr(ide_diag_mod, "registry"), \
        "ide_diag 丢失 import registry → _lsp_file_diags/_clippy_diags NameError 被吞"


def test_tool_binding_not_misbound():
    """S52 教训：拆分后 @tool 必须绑在同名 handler 上。"""
    h = registry._TOOLS["ide_diagnostics"]["handler"]
    assert h.__name__ == "ide_diagnostics"


# ---------- _lsp_file_diags ----------

def test_lsp_file_diags_maps_to_unified_shape(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(ide_diag_mod, "registry", _fake_registry(lsp_diags=[
        {"severity": "error", "line": 4, "message": "boom", "source": "pylsp"},
        {"severity": "weird", "line": 0, "message": "m2"},
    ]))
    diags, eng, _skip = ide_diag_mod._lsp_file_diags(str(tmp_path), "a.py")
    assert eng == "python-lsp"
    assert diags[0]["line"] == 5 and diags[0]["severity"] == "error"  # 1-based
    assert diags[0]["file"] == "a.py" and diags[0]["source"] == "pylsp"
    assert diags[1]["severity"] == "warning"  # 未知 severity 归一为 warning


def test_lsp_file_diags_missing_file_and_unsupported_lang(tmp_path):
    assert ide_diag_mod._lsp_file_diags(str(tmp_path), "ghost.py") == ([], None, None)
    assert ide_diag_mod._lsp_file_diags(str(tmp_path), "a.c") == ([], None, None)


def test_lsp_file_diags_lsp_down_is_honest_skip(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(ide_diag_mod, "registry",
                        _fake_registry(lsp_raises=True))
    diags, eng, _skip = ide_diag_mod._lsp_file_diags(str(tmp_path), "a.py")
    assert diags == [] and eng is None


# ---------- _clippy_diags ----------

def test_clippy_skips_non_cargo(tmp_path):
    assert ide_diag_mod._clippy_diags(str(tmp_path)) == ([], None, None)


def test_clippy_maps_warnings(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.setattr(ide_diag_mod, "registry", _fake_registry(clippy={
        "warnings": [{"file": os.path.join(str(tmp_path), "src", "lib.rs"),
                      "line": 2, "level": "warning", "msg": "unused"}]}))
    diags, eng, _skip = ide_diag_mod._clippy_diags(str(tmp_path))
    assert eng == "clippy"
    assert diags[0]["file"] == "src/lib.rs" and diags[0]["line"] == 2


# ---------- ide_diagnostics 聚合 ----------

def test_ide_diagnostics_aggregates(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.setattr(ide_diag_mod, "registry", _fake_registry(
        lsp_diags=[{"severity": "error", "line": 0, "message": "e1"}],
        clippy={"warnings": [{"file": os.path.join(str(tmp_path), "lib.rs"),
                              "line": 1, "level": "warning", "msg": "w1"}]}))
    monkeypatch.setattr(ide_diag_mod, "_which", lambda name: None)  # 锁 linter 面
    r = ide_diag_mod.ide_diagnostics(str(tmp_path), files=["a.py"],
                                     __authorized=True)
    assert r["total"] == 2 and r["errors"] == 1
    assert set(r["engine"].split("+")) == {"python-lsp", "clippy"}
    assert any("ruff" in x for x in r["skipped"]), r   # S130：缺席可见


def test_ide_diagnostics_via_registry(tmp_path, monkeypatch):
    """registry 层往返：files 参数此前因装饰器错绑被 TypeError 拒绝（S52）。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(ide_diag_mod, "registry", _fake_registry())
    monkeypatch.setattr(ide_diag_mod, "_which", lambda name: None)  # 探测面确定化
    r = registry.call("ide_diagnostics",
                        {"path": str(tmp_path), "files": ["a.py"],
                         "__authorized": True})  # S130：升执行类挂门
    assert r["ok"] and r["result"]["total"] == 0
    assert "skipped" in r["result"]


def test_clippy_refusal_is_visible_not_silent(tmp_path, monkeypatch):
    """S130 实锤缺陷回归门：内层调用被授权门拒绝时 skip 原因必须可见
    （旧实现静默吞成 engine=none 空信号）。"""
    (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")

    def refuse(name, args):
        if name == "ide_build":
            return {"ok": False, "error": "PermissionError: 需要授权"}
        return {"ok": True, "result": {}}
    monkeypatch.setattr(ide_diag_mod, "registry", SimpleNamespace(call=refuse))
    diags, eng, skip = ide_diag_mod._clippy_diags(str(tmp_path))
    assert diags == [] and eng is None
    assert skip and "授权" in skip, skip


def test_python_linter_probe_parsers():
    """S130/P2-A：三个 linter 解析器（canned 输出，零环境依赖）。"""
    ruff = ide_diag_mod._parse_ruff_json(
        '[{"filename": "C:/p/a.py", "location": {"row": 3, "column": 5},'
        ' "code": "F401", "message": "unused import"},'
        ' {"filename": "C:/p/b.py", "location": {"row": 1, "column": 1},'
        ' "code": "E999", "message": "syntax"}]')
    assert ruff[0]["source"] == "ruff" and ruff[0]["line"] == 3
    assert ruff[0]["severity"] == "warning" and "F401" in ruff[0]["message"]
    assert ruff[1]["severity"] == "error", "E9* 语法级应为 error"
    pf = ide_diag_mod._parse_pyflakes(
        "C:/p/a.py:7:1: undefined name 'z'" + chr(10) + "noise")
    assert pf[0]["source"] == "pyflakes" and pf[0]["line"] == 7
    my = ide_diag_mod._parse_mypy(
        "C:/p/a.py:4: error: Incompatible types" + chr(10)
        + "C:/p/a.py:9: note: Revealed type" + chr(10) + "bad line")
    assert [d["severity"] for d in my] == ["error", "info"], my
    assert len(my) == 2


def test_python_linter_absence_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_diag_mod, "_which", lambda name: None)
    diags, engines, skipped = ide_diag_mod._python_linter_diags(str(tmp_path))
    assert diags == [] and engines == []
    assert len(skipped) == 2 and all("未安装" in x for x in skipped), skipped
