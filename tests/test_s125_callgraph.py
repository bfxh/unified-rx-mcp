# -*- coding: utf-8 -*-
"""S125 调用图契约：ide_callgraph（薄壳 rx-scan callgraph + 查询层）。

夹具场景与 Rust 侧 rust/tests/callgraph_test.rs 同源——两端各锁一次：
Rust 锁引擎事实，本文件锁工具面（注册/沙盒/exe 纪律/查询层形状/汇总）。

夹具写法：pathlib 字面量组件（tmp_path / "包" / "文件.py"），不做字符串
路径拼接——夹具 helper 也不长得像穿越面。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import ide_callgraph as icg

_HAS_EXE = icg._rx_scan_exe() is not None
_EXE_HINT = "rx-scan.exe 未构建（cargo build --release）"


def _w(path, content):
    """写夹具文件（path 由调用点以 pathlib 字面量组件构造）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _fixture_pkg(root):
    """a→b→c 链 + 互递归对 + self 方法 + 未解析面。"""
    _w(root / "pkg" / "__init__.py", "")
    _w(root / "pkg" / "mod.py",
       "from .util import helper\n\n"
       "def a():\n    return b()\n\n"
       "def b():\n    return c()\n\n"
       "def c():\n    return helper()\n\n"
       "def even(n):\n    return odd(n - 1)\n\n"
       "def odd(n):\n    return even(n - 1)\n\n"
       "x = a()\n")
    _w(root / "pkg" / "util.py", "def helper():\n    return 1\n")
    _w(root / "pkg" / "klass.py",
       "import os\n\n"
       "class C:\n"
       "    def m(self):\n        return self.helper()\n\n"
       "    def helper(self):\n        return os.getcwd()\n")


def _call(root, **kw):
    return registry.call("ide_callgraph", {"root": str(root), **kw})


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_summary_stats_self_consistent(tmp_path):
    _fixture_pkg(tmp_path)
    r = _call(tmp_path)
    assert r.get("ok"), r
    res = r["result"]
    assert res["engine"] == "rust:nameres-callgraph" and res["mode"] == "summary"
    st = res["stats"]
    assert st["calls"] == st["resolved"] + st["unresolved"] + st["builtin_calls"], st
    assert 0.0 <= st["resolution_rate"] <= 1.0, st
    assert res["top_fan_in"] and res["top_fan_out"], res


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_query_callers_with_depth(tmp_path):
    _fixture_pkg(tmp_path)
    # 真实符号：b 的调用者（depth 1 = a 一处；depth 2 追加模块级 x = a()）
    r1 = _call(tmp_path, symbol="pkg.mod.b", direction="callers", depth=1)
    assert r1.get("ok"), r1
    assert [e["caller"] for e in r1["result"]["callers"]] == ["pkg.mod.a"], r1["result"]
    r2 = _call(tmp_path, symbol="pkg.mod.b", direction="callers", depth=2)
    callers2 = sorted(e["caller"] for e in r2["result"]["callers"])
    assert callers2 == ["", "pkg.mod.a"], r2["result"]
    assert "callees" not in r2["result"], "direction=callers 不应带 callees"


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_query_callees_depth(tmp_path):
    _fixture_pkg(tmp_path)
    r = _call(tmp_path, symbol="pkg.mod.a", direction="callees", depth=2)
    assert r.get("ok"), r
    pairs = sorted((e["callee"], e["depth"]) for e in r["result"]["callees"])
    assert ("pkg.mod.b", 1) in pairs and ("pkg.mod.c", 2) in pairs, pairs


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_ambiguous_symbol_lists_candidates(tmp_path):
    _w(tmp_path / "p1.py", "def run():\n    return 1\n")
    _w(tmp_path / "p2.py", "def run():\n    return 2\n")
    r = _call(tmp_path, symbol="run")
    assert r.get("ok"), r
    res = r["result"]
    assert res.get("count") == 2 and res["ambiguous"] == ["p1.run", "p2.run"], res
    # 全限定名消歧后正常出结果
    r2 = _call(tmp_path, symbol="p1.run", direction="callers")
    assert r2.get("ok") and r2["result"]["symbol"] == "p1.run" and "ambiguous" not in r2["result"]


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_not_found_has_hint(tmp_path):
    _w(tmp_path / "t.py", "def f():\n    return 1\n")
    r = _call(tmp_path, symbol="nope")
    # 符号取不到 = 清晰错误包络（registry 把 result["error"] 转 ok:false，同 ide_impact 口径）
    assert r.get("ok") is False and "not_found" in str(r["error"]), r
    assert r["result"]["error"] == "not_found" and "hint" in r["result"], r["result"]


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_cycles_and_self_method_and_reasons(tmp_path):
    _fixture_pkg(tmp_path)
    res = _call(tmp_path)["result"]
    # 环：even↔odd（规范形去重后有且仅有它）
    cycles = res["cycles"]
    assert any(len(c["cycle"]) == 2 and set(c["cycle"]) == {"pkg.mod.even", "pkg.mod.odd"}
               for c in cycles), cycles
    # self 方法解析 + 外部调用分类
    r = _call(tmp_path, symbol="pkg.klass.C.m", direction="callees", depth=1)["result"]
    assert [e["callee"] for e in r["callees"]] == ["pkg.klass.C.helper"], r
    by_reason = res["stats"]["by_reason"]
    assert by_reason.get("external", 0) >= 1, by_reason


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_sandbox_rejects_outside(tmp_path):
    # 未授权路径必须拒绝（fail-closed 门在工具入口）
    r = registry.call("ide_callgraph", {"root": r"C:\Windows"})
    assert r.get("ok") is False and "沙盒" in r["error"], r


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_wrapper_deterministic_two_runs(tmp_path):
    # 直连包装层（绕过 registry 缓存）——两次输出逐字节一致
    _fixture_pkg(tmp_path)
    import json
    j1 = json.dumps(icg._rx_callgraph(str(tmp_path), 100), sort_keys=True, ensure_ascii=False)
    j2 = json.dumps(icg._rx_callgraph(str(tmp_path), 100), sort_keys=True, ensure_ascii=False)
    assert j1 == j2


def test_missing_exe_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(icg, "_rx_scan_exe", lambda: None)
    r = registry.call("ide_callgraph", {"root": str(tmp_path)})
    assert r.get("ok") is False and "rx-scan.exe" in r["error"], r


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_repo_smoke_known_symbol():
    # 真仓冒烟：本仓 tools 包（conftest 沙盒含仓库根）
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    r = registry.call("ide_callgraph", {"root": root, "symbol": "tools.fs._resolve",
                                        "direction": "callers", "depth": 1, "max_files": 40})
    assert r.get("ok"), r
    res = r["result"]
    assert res["symbol"] == "tools.fs._resolve" and res["counts_callers"] >= 5, res.get("counts_callers")
