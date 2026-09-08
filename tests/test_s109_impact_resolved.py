# -*- coding: utf-8 -*-
"""S109 ide_impact 三级降级契约：LSP（语义）→ 解析（名字解析）→ 文本（全文计数）。

本机 pylsp 未装（S99 起如实报 detected=false），因此默认路径即解析级——测试
按层 monkeypatch，逐级锁定优先级与输出形状。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import lsp as lsp_mod
from tools import scan as scan_mod


def _mkpkg(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def target():\n    return 1\n\n\nvalue = target()\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text(
        "from pkg.a import target\n\n\ndef use():\n    return target()\n",
        encoding="utf-8")
    return tmp_path / "pkg" / "a.py"


def _no_lsp(monkeypatch):
    """强制 LSP 层不可用（不依赖环境是否装 pylsp——3.11 本机装了）。"""
    def boom(*a, **k):
        raise ConnectionError("server closed")
    monkeypatch.setattr(lsp_mod, "ide_lsp", boom)


def _call(file, line=0, col=4):
    r = registry.call("ide_impact", {"file": str(file), "line": line, "col": col})
    assert r.get("ok"), r
    return r["result"]


@pytest.mark.skipif(scan_mod._rx_scan_exe() is None, reason="rx-scan.exe 缺失")
def test_resolved_tier(tmp_path, monkeypatch):
    _no_lsp(monkeypatch)
    a = _mkpkg(tmp_path)
    res = _call(a)
    assert res["engine"] == "resolved", res
    assert res["symbol"] == "target", res
    files = {os.path.basename(f["file"]): f for f in res["files"]}
    assert "b.py" in files, res          # 跨文件：import 行
    assert files["b.py"]["lines"] == [1], files["b.py"]
    assert "a.py" in files, res          # 同文件：精确引用行（第 5 行 value = target()）
    assert 5 in files["a.py"]["lines"], files["a.py"]


def test_lsp_tier_precedence(tmp_path, monkeypatch):
    a = _mkpkg(tmp_path)
    monkeypatch.setattr(lsp_mod, "ide_lsp", lambda *x, **k: {
        "engine": "lsp", "total": 2,
        "references": [{"file": str(a), "line": 4},
                       {"file": str(a.parent / "b.py"), "line": 4}]})
    res = _call(a)
    assert res["engine"] == "lsp", res
    assert res["total_refs"] == 2, res


def test_text_tier_when_resolution_unavailable(tmp_path, monkeypatch):
    _no_lsp(monkeypatch)
    a = _mkpkg(tmp_path)
    monkeypatch.setattr(lsp_mod, "_resolved_impact", lambda *x, **k: None)
    res = _call(a)
    assert res["engine"] == "text", res
    assert "fallback_reason" in res, res


def test_no_ident_falls_through_to_text_error(tmp_path, monkeypatch):
    _no_lsp(monkeypatch)
    f = tmp_path / "m.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ide_impact", {"file": str(f), "line": 1, "col": 0})
    # 第 2 行（0-based 1）不存在/无标识符 → 文本兜底报清晰错误（不抛穿）
    assert r.get("ok") is False, r
    assert "取不到符号" in str(r.get("error")) or "无标识符" in str(r.get("error")), r
