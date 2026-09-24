"""真 LSP 服务器 e2e（慢速桶）：pylsp / rust-analyzer 在场才跑，缺席如实跳过。

此前所有 LSP 测试都走 fake server——协议对但语义零覆盖（服务器真解析代码后
给出的定义/引用/诊断从未被测过）。本桶用真服务器补上；冷启动索引慢是已知
成本（pylsp 秒级、rust-analyzer 首响 ~17s），单独成文件可整文件跳过。
"""
import os
import shutil
import sys
import time

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
# 注：definition/completion 走 pylsp 内建 jedi 插件（无独立 pylsp_jedi 包）。
# 旧注记曾写"jedi 0.20 与 pylsp 1.15 不兼容（goto 空）⇒ 环境钉 0.19.2"——
# **2026-09-24 实测否证**：jedi 0.20.0 + pylsp 1.15.0 下本文件 4 条全绿
# （definition 拿到真位置、completion 47/47 位置非空），故不再钉小版本，
# ci-requirements 里保持 `jedi` 浮动 = 取最新。
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


@pytest.mark.skipif(not HAS_PY, reason="pylsp 未安装")
def test_pylsp_real_completion(tmp_path, monkeypatch):
    """真补全（S171 接线）：**判据不是"非空"而是"有没有那个属性"**——
    空列表与"给了一堆无关标签"在"非空"口径下都算过，那种门等于装饰。

    位置带前缀（`os.pa`）：实测点号后**无前缀**时服务器给整个命名空间（`os.` → 462 条、
    字母序），前 100 条自然轮不到 `path`；带前缀才是确定性判据（也才是真实用法）。
    """
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setattr(lsp_mod, "_SESSIONS", {})
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "m.py"
    f.write_text("import os\n\n\nos.pa\n", encoding="utf-8")
    try:
        r, labels = None, []
        for _ in range(4):                      # 冷启动退避（同 definition 那条）
            r = registry.call("ide_lsp", {"action": "completion",
                                          "file": str(f), "line": 3, "col": 5})
            assert r["ok"], r.get("error")
            labels = [str(i["label"]) for i in r["result"]["items"]]
            if any(lb.startswith("path") for lb in labels):
                break
            time.sleep(3)
        assert labels, "真 pylsp 补全返回空"
        assert any(lb.startswith("path") for lb in labels), \
            f"os.pa 之后没给出 path（前 5：{labels[:5]}）"
        item = r["result"]["items"][0]
        assert {"label", "kind", "detail"} <= set(item), item
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
        # S125：冷启动索引竞态——工具内已有 19s 退避重试，CI 冷 runner 上 ra 首索
        # 仍可能超预算（实测 3.14 首跑 references 空）。测试侧加有界重试（会话在
        # _SESSIONS 缓存里，重试即温调用），最多 3 轮；仍空才判失败。
        refs = []
        for _attempt in range(3):
            r = registry.call("ide_lsp", {"action": "references", "file": str(lib),
                                          "line": 0, "col": 7,
                                          "include_decl": True})
            assert r["ok"], r.get("error")
            refs = r["result"]["references"]
            if len(refs) >= 2:
                break
            time.sleep(5)
        assert len(refs) >= 2, f"ra 真引用过少: {refs}"
        assert any(x["file"].endswith("lib.rs") for x in refs)
    finally:
        _stop(lsp_mod)
