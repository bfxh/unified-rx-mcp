"""S129：ide_impact 调用面档（`calls` 段）契约——引用面 ≠ 调用面，分层不混。

夹具带 .git 标记让 `_session_root` 认包根（from pkg.a import target 才可解析）；
调用面不可用时如实缺席（不拖垮主结果）。
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import scan as scan_mod  # noqa: E402

pytestmark = pytest.mark.skipif(scan_mod._rx_scan_exe() is None,
                                reason="rx-scan.exe 缺失")


def _mkpkg(tmp_path):
    (tmp_path / ".git").mkdir()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    a = pkg / "a.py"
    a.write_text("def target():\n    return 1\n\n\ndef local():\n    return target()\n",
                 encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import target\n\n\ndef use():\n    return target()\n",
        encoding="utf-8")
    return a


def _call(a, **extra):
    r = registry.call("ide_impact", {"file": str(a), "line": 0, "col": 4, **extra})
    assert r.get("ok"), r
    return r["result"]


def test_calls_section_contract(tmp_path, monkeypatch):
    # LSP 强制不可用（默认路径即解析级，调用面独立于降级链）
    import tools.impact as im
    monkeypatch.setattr(im, "ide_lsp", lambda *a, **k: {"error": "pylsp 未安装"})
    a = _mkpkg(tmp_path)
    res = _call(a)
    assert res["engine"] == "resolved", res
    calls = res.get("calls")
    assert calls, "调用面段应存在: %r" % res
    assert calls["engine"] == "rust:nameres-callgraph"
    assert calls["call_sites"] >= 2, calls      # 同文件 + 跨文件各一条
    assert calls["def_line_aligned"] is True, calls
    files = {os.path.basename(c["file"]) for c in calls["callers"]}
    assert "b.py" in files, calls               # 跨文件调用点（from-import 解析）
    assert "a.py" in files, calls               # 同文件调用点
    assert "引用≠调用" in calls["note"], calls


def test_calls_false_omits_section(tmp_path, monkeypatch):
    import tools.impact as im
    monkeypatch.setattr(im, "ide_lsp", lambda *a, **k: {"error": "pylsp 未安装"})
    a = _mkpkg(tmp_path)
    res = _call(a, calls=False)
    assert "calls" not in res, res
    assert res["engine"] == "resolved", res     # 主结果不受影响


def test_calls_absent_when_callgraph_unavailable(tmp_path, monkeypatch):
    import tools.impact as im
    monkeypatch.setattr(im, "ide_lsp", lambda *a, **k: {"error": "pylsp 未安装"})
    monkeypatch.setattr(im, "_call_impact", lambda *a, **k: None)
    a = _mkpkg(tmp_path)
    res = _call(a)
    assert "calls" not in res, res
    assert res["engine"] == "resolved", res


def test_schema_declares_calls_and_old_shape_kept():
    schema = next(t["inputSchema"] for t in registry.list_tools()
                  if t["name"] == "ide_impact")
    props = schema.get("properties") or {}
    assert "calls" in props, "calls 参数必须入 schema"
    for k in ("file", "line", "col", "include_decl"):
        assert k in props, k
