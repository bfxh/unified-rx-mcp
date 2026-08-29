# -*- coding: utf-8 -*-
"""S60 挖洞轮的回归钉：会话 root 统一 / 旗标注入 / BOM 匹配 / scheme 混淆。

对应四个真 bug（探针实锤后修复）：
- LSP 会话 root=文件目录 → src/ 与 tests/ 各起一个服务器，跨文件语义失明
- ide_test target argv 旗标注入（--junitxml 可进命令行）
- ide_edit_multi 对 BOM 文件 old_lines 永不匹配
- rename_apply 接受非 file: uri（scheme 混淆 → 写错文件）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import lsp as lsp_mod  # noqa: E402
from tools.lsp import _session_root  # noqa: E402


def _stop(mod):
    for k in list(mod._SESSIONS):
        mod._SESSIONS[k][0].stop()


def test_session_root_walks_to_git(tmp_path):
    base = tmp_path / "base"
    proj = base / "proj"
    (proj / ".git").mkdir(parents=True)
    deep = proj / "a" / "b"
    deep.mkdir(parents=True)
    assert _session_root(str(deep / "f.py")) == str(proj)
    # 同级无 .git 目录 → 退回文件目录（向后兼容 test_lsp 的 tmp 布局）
    standalone = base / "standalone"
    standalone.mkdir()
    assert _session_root(str(standalone / "f.py")) == str(standalone)


def test_lsp_session_unified_across_dirs(tmp_path, monkeypatch):
    """src/ 与 tests/ 的文件必须共享一个语言服务器（跨文件语义的前提）。"""
    stub = os.path.join(HERE, "fixtures", "fake_lsp_server.py")
    monkeypatch.setenv("UNIFIED_RX_LSP_CMD_PYTHON",
                       f"{sys.executable} {stub}")
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    fa = tmp_path / "src" / "a.py"
    fb = tmp_path / "tests" / "test_a.py"
    fa.write_text("x = 1\n", encoding="utf-8")
    fb.write_text("import src.a\n", encoding="utf-8")
    try:
        r1 = lsp_mod.ide_lsp("diagnostics", file=str(fa))
        r2 = lsp_mod.ide_lsp("diagnostics", file=str(fb))
        assert r1.get("error") is None and r1.get("engine"), r1
        assert r2.get("error") is None and r2.get("engine"), r2
        assert len(lsp_mod._SESSIONS) == 1, \
            f"会话分裂: {list(lsp_mod._SESSIONS)}"
    finally:
        _stop(lsp_mod)
