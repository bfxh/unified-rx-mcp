# -*- coding: utf-8 -*-
"""S61 硬化轮回归钉：动态执行 AST 规则 / 旗标后授权统一 / fuzzy 匹配 /
尺寸护栏 / 动态 import 依赖边。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools.metrics import _imports_of  # noqa: E402

AUTH = {"__authorized": True}


# ---------- bug_scan python 动态执行（AST 级） ----------

def test_bug_scan_flags_bare_eval_exec(tmp_path):
    (tmp_path / "evil.py").write_text(
        "def f(u):\n    return eval(u)\n\ndef g(u):\n    exec(u)\n",
        encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    hits = [i for i in r["result"]["issues"] if i["rule"] == "eval_exec"]
    assert len(hits) == 2
    assert all(i["kind"] == "definite" and i["severity"] == "high" for i in hits)


def test_bug_scan_not_flagging_member_calls(tmp_path):
    """S44 dsml FP 教训的 AST 解法：re.compile 等 Attribute 成员调用不算动态执行。"""
    (tmp_path / "ok.py").write_text(
        "import re\nP = re.compile(r'x')\ns = 'x'.replace('x', 'y')\n",
        encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    hits = [i for i in (r.get("result") or {}).get("issues") or []
            if i["rule"] == "eval_exec"]
    assert hits == [], f"成员调用误报: {hits}"


def test_registry_error_key_always_means_failure(tmp_path):
    """S61 契约钉：error 键 dict 无论几键都转 ok:false——0 应用不再是假成功。"""
    f = tmp_path / "f.py"
    f.write_text("a = 1\n", encoding="utf-8")
    r = registry.call("ide_edit_multi", {**AUTH, "file_path": str(f),
                                         "edits": [{"old_lines": ["ghost"],
                                                    "new_lines": ["x"]}]})
    assert r["ok"] is False and "0 应用" in r["error"]


# ---------- 授权门（执行类工具） ----------

def test_exec_tools_require_auth(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    for name, args in [
            ("ide_build", {"path": str(tmp_path)}),
            ("ide_test", {"path": str(tmp_path)}),
            ("ide_debug", {"path": str(tmp_path), "cmd": ["python", "-c", "1"]}),
            ("ide_break", {"path": str(tmp_path), "cmd": ["python", "-c", "1"],
                           "breakpoints": []}),
            ("ide_doctor", {"path": str(tmp_path)})]:
        r = registry.call(name, args)
        assert not r["ok"] and "授权" in r["error"], name
        r2 = registry.call(name, {**args, "__authorized": True})
        assert r2.get("ok") is not None, f"{name} 授权后仍被拒: {r2}"


def test_registry_strips_auth_for_non_auth_tools(tmp_path):
    """__authorized 对非 auth 工具统一无害（registry 剥离，不撑爆签名）。"""
    r = registry.call("code_context", {**AUTH, "path": __file__})
    assert r["ok"], r.get("error")


# ---------- fuzzy 匹配 ----------

def test_edit_fuzzy_whitespace_tolerant(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("def a():\n    x = 1\n    return x\n", encoding="utf-8")
    # 行中夹全空白行 + 缩进差异 → 精确匹配失败，fuzzy 命中
    edits = [{"old_lines": ["x = 1", "  return x"],
              "new_lines": ["    x = 2", "    return x"]}]
    r_exact = registry.call("ide_edit_multi", {**AUTH, "file_path": str(f),
                                               "edits": edits})
    assert r_exact["ok"] is False  # 精确不命中（缩进不同）
    r = registry.call("ide_edit_multi", {**AUTH, "file_path": str(f),
                                         "edits": edits, "fuzzy": True})
    assert r["ok"] and r["result"]["applied"] == 1
    assert "x = 2" in f.read_text(encoding="utf-8")


# ---------- 尺寸护栏 ----------

def test_edit_refuses_oversized_file(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" + "y = 2\n" * (11 * 1024 * 1024 // 6), encoding="utf-8")
    assert os.path.getsize(f) > 10 * 1024 * 1024
    r = registry.call("ide_edit_multi", {**AUTH, "file_path": str(f),
                                         "edits": [{"old_lines": ["y = 2"],
                                                    "new_lines": ["y = 3"]}]})
    assert not r["ok"] and "拒绝" in r["error"]
    r2 = registry.call("code_context", {"path": str(f)})
    assert not r2["ok"] and "拒绝" in r2["error"]


# ---------- dep_graph 动态 import ----------

def test_dep_graph_sees_dynamic_imports(tmp_path):
    (tmp_path / "a_mod.py").write_text(
        "import importlib\nm = importlib.import_module('b_mod')\n"
        "raw = __import__('c_mod')\n", encoding="utf-8")
    (tmp_path / "b_mod.py").write_text("v = 1\n", encoding="utf-8")
    (tmp_path / "c_mod.py").write_text("v = 2\n", encoding="utf-8")
    r = registry.call("dep_graph", {"path": str(tmp_path)})
    assert r["ok"]
    g = r["result"]["graph"]["a_mod.py"]
    assert "b_mod" in g and "c_mod" in g, g
