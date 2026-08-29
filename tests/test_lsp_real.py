# -*- coding: utf-8 -*-
"""真 LSP 服务器 e2e（慢速桶）：pylsp / rust-analyzer 在场才跑，缺席如实跳过。

此前所有 LSP 测试都走 fake server——协议对但语义零覆盖（服务器真解析代码后
给出的定义/引用/诊断从未被测过）。本桶用真服务器补上；冷启动索引慢是已知
成本（pylsp 秒级、rust-analyzer 首响 ~17s），单独成文件可整文件跳过。
"""
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import lsp as lsp_mod  # noqa: E402

HAS_PY = False
try:
    import pylsp  # noqa: F401
    HAS_PY = True
except Exception:
    pass
import importlib.util as _iu
HAS_FLAKES = _iu.find_spec("pyflakes") is not None   # 诊断靠 pyflakes
# 注：definition 走 pylsp 内建 jedi 插件（无独立 pylsp_jedi 包）；
# jedi 0.20.0 与 pylsp 1.15 不兼容（goto 空），环境已钉 0.19.2
HAS_RA = shutil.which("rust-analyzer") is not None


def _stop(mod):
    for k in list(mod._SESSIONS):
        mod._SESSIONS[k][0].stop()


@pytest.mark.skipif(not HAS_PY, reason="pylsp 未安装")
def test_pylsp_real_diagnostics_and_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "m.py"
    f.write_text("def target_fn():\n    return 42\n\n\ntarget_fn()\n",
                 encoding="utf-8")
    try:
        # 真服务器冷启动慢（jedi 首响可超 19s 退避窗）——热会话重试是标准姿势
        # 调用点在 0-based 第 4 行（两个空行使然）
        locs = []
        for _ in range(4):
            r = registry.call("ide_lsp", {"action": "definition",
                                          "file": str(f), "line": 4, "col": 0})
            assert r["ok"], r.get("error")
            locs = r["result"]["locations"]
            if locs:
                break
            import time
            time.sleep(3)
        assert locs and locs[0]["line"] == 0, \
            f"pylsp 真定义跳转失败: {locs}"
        # 真诊断：语法错误必须被真服务器报出
        bad = proj / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        r2 = registry.call("ide_lsp", {"action": "diagnostics",
                                       "file": str(bad)})
        assert r2["ok"], r2.get("error")
        assert r2["result"]["total"] >= 1, "真 pylsp 未报语法错误"
    finally:
        _stop(lsp_mod)


@pytest.mark.skipif(not (HAS_PY and HAS_FLAKES), reason="pylsp/pyflakes 未安装")
def test_pylsp_real_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    proj = tmp_path / "proj"
    proj.mkdir()
    bad = proj / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    try:
        r2 = registry.call("ide_lsp", {"action": "diagnostics",
                                       "file": str(bad)})
        assert r2["ok"], r2.get("error")
        assert r2["result"]["total"] >= 1, "真 pylsp 未报语法错误"
    finally:
        _stop(lsp_mod)


@pytest.mark.skipif(not HAS_RA, reason="rust-analyzer 未安装")
def test_rust_analyzer_real_references(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    proj = tmp_path / "crate"
    (proj / "src").mkdir(parents=True)
    (proj / "Cargo.toml").write_text(
        '[package]\nname = "urxra"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8")
    lib = proj / "src" / "lib.rs"
    lib.write_text(
        "pub fn anchor_fn() -> u32 { 1 }\n"
        "pub fn caller() -> u32 { anchor_fn() }\n", encoding="utf-8")
    try:
        r = registry.call("ide_lsp", {"action": "references", "file": str(lib),
                                      "line": 0, "col": 7,
                                      "include_decl": True})
        assert r["ok"], r.get("error")
        refs = r["result"]["references"]
        assert len(refs) >= 2, f"ra 真引用过少: {refs}"
        assert any(x["file"].endswith("lib.rs") for x in refs)
    finally:
        _stop(lsp_mod)
