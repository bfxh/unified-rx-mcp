# -*- coding: utf-8 -*-
"""S99 ide 域两修契约：①LSP 检测诚实化；②ide_impact 文本级降级。

实锤背景（S99 探针）：`ide_lsp status` 对 python 的探测只做 exe 的 which/存在性
检查——而 pylsp 的 exe 是解释器本身，于是**没装 pylsp 也报 detected=true**
（违反 README"缺失时如实报 detected=false，绝不假装支持"）；随后 `ide_impact`
起会话直接 ConnectionError 抛穿（registry 只看到异常字符串）。

契约：
- 检测：`python -m <mod>` 形态必须再验模块存在；缺失 → detected=False + reason。
  自定义 cmd（env 覆盖）维持 which 口径，不受模块判影响。
- 降级：LSP 不可用（异常或 error 结果）→ 文本级兜底（engine="text" + symbol +
  fallback_reason），文件聚合形状与 LSP 路径一致（files[].refs/has_test，lines 空）；
  LSP 路径行为不变（engine 透传、无 fallback_reason）。
"""
import sys

import pytest

import registry
import tools  # noqa: F401
from tools import ide_read
from tools import lsp as lsp_mod


def _status():
    r = registry.call("ide_lsp", {"action": "status"})
    assert r.get("ok"), r
    return r["result"]["servers"]


def test_status_python_module_missing_not_detected(monkeypatch):
    monkeypatch.delenv("UNIFIED_RX_LSP_CMD_PYTHON", raising=False)
    monkeypatch.setattr(lsp_mod, "_module_available", lambda name: False)
    s = _status()
    assert s["python"]["detected"] is False, s
    assert "pylsp" in s["python"].get("reason", ""), s


def test_status_custom_cmd_detected(tmp_path, monkeypatch):
    stub = tmp_path / "stub_lsp.py"
    stub.write_text("import sys\n", encoding="utf-8")
    monkeypatch.setenv("UNIFIED_RX_LSP_CMD_PYTHON", f"{sys.executable} {stub}")
    # 自定义 cmd 不走 -m 模块判（模块可用性判 False 也不影响）
    monkeypatch.setattr(lsp_mod, "_module_available", lambda name: False)
    s = _status()
    assert s["python"]["detected"] is True, s
    assert "reason" not in s["python"], s


def test_impact_lsp_path_shape_unchanged(monkeypatch, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def target():\n    pass\n", encoding="utf-8")
    other = tmp_path / "b.py"
    monkeypatch.setattr(lsp_mod, "ide_lsp", lambda *a, **k: {
        "engine": "lsp", "total": 2,
        "references": [{"file": str(other), "line": 3},
                       {"file": str(f), "line": 1}]})
    r = lsp_mod.ide_impact(str(f), 0, 4)
    assert r.get("engine") == "lsp", r
    assert r["total_refs"] == 2 and len(r["files"]) == 2, r
    assert "fallback_reason" not in r, r


def test_impact_text_fallback_when_lsp_unavailable(monkeypatch, tmp_path):
    if ide_read._rx_ide_exe() is None:
        pytest.skip("rx-ide.exe 缺失（文本兜底走 rx-ide 计数）")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    a = pkg / "a.py"
    a.write_text("def target():\n    return 1\n", encoding="utf-8")
    b = pkg / "b.py"
    b.write_text("from a import target\n\ntarget()\n", encoding="utf-8")

    def boom(*a_, **k_):
        raise ConnectionError("server closed")

    monkeypatch.setattr(lsp_mod, "ide_lsp", boom)
    # S109：三级降级——本测试显式关掉解析级，专测最底层的文本兜底
    monkeypatch.setattr(lsp_mod, "_resolved_impact", lambda *x, **k: None)
    r = lsp_mod.ide_impact(str(a), 0, 4)
    assert r.get("engine") == "text", r
    assert r.get("symbol") == "target", r
    assert "ConnectionError" in r.get("fallback_reason", ""), r
    files = {x["file"] for x in r["files"]}
    assert str(a) in files and str(b) in files, r
    assert r["total_refs"] >= 2, r
    assert all(x["lines"] == [] for x in r["files"]), r


def test_impact_text_fallback_no_ident(monkeypatch, tmp_path):
    f = tmp_path / "empty.py"
    f.write_text("\n\n", encoding="utf-8")
    monkeypatch.setattr(lsp_mod, "ide_lsp",
                        lambda *a, **k: {"error": "pylsp 未安装"})
    r = lsp_mod.ide_impact(str(f), 0, 0)
    assert "error" in r and "文本兜底也取不到符号" in r["error"], r
