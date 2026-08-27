# -*- coding: utf-8 -*-
"""S9 结构化扫描测试：AST/词法层对抗样例——正则层的每个坑在这里都要有对应的反例。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
import tools.astscan as asx  # noqa: E402


def _js_scan(tmp_path, code):
    f = tmp_path / "t.js"
    f.write_text(code, encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert r["ok"], r
    return r["result"]


def _rules(res):
    return [i["rule"] for i in res["issues"]]


# ---------- JS 掩码层：字符串/注释里的 sink 不算 ----------

def test_js_string_and_comment_never_flagged(tmp_path):
    res = _js_scan(tmp_path, 'const a = "eval(x)";\n// exec(cmd)\nconst b = "exec(c)";\n')
    assert _rules(res) == [], res


def test_js_regex_member_exec_not_flagged(tmp_path):
    """误报根源案例：RegExp.prototype.exec 是成员调用。"""
    res = _js_scan(tmp_path, "const m = /a(b)/.exec(text);\n")
    assert "js_dynamic_exec" not in _rules(res)


def test_js_bare_exec_flagged_with_position(tmp_path):
    res = _js_scan(tmp_path, "const x=1;\nexec(userCmd);\n")
    hits = [i for i in res["issues"] if i["rule"] == "js_dynamic_exec"]
    assert len(hits) == 1 and hits[0]["line"] == 2 and hits[0]["callee"] == "exec"


def test_js_new_function_flagged(tmp_path):
    res = _js_scan(tmp_path, 'const f = new Function("return 1");\n')
    assert "js_new_function" in _rules(res)


def test_js_template_interpolation_is_code(tmp_path):
    """`${...}` 插值是真执行的代码——掩码不能吞掉，否则漏报。"""
    res = _js_scan(tmp_path, 'const s = `hello ${exec(cmd)} world`;\n')
    assert "js_dynamic_exec" in _rules(res)


def test_js_call_total_counts_words(tmp_path):
    res = _js_scan(tmp_path, "foo(); bar(1); baz(x.y());\n")
    stats = res["units"][0]
    # 调用面总数含嵌套成员调用 x.y() —— 它也是真调用表达式
    assert stats["calls_total"] == 4, stats
    # 但成员调用不产生 dynamic_exec 问题条目
    assert res["total"] == 0


# ---------- Python AST 层 ----------

def test_py_bare_eval_dynamic_vs_literal(tmp_path):
    f = tmp_path / "t.py"
    f.write_text('eval("1+1")\nexec(user_input)\n', encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    hits = {i.get("arg_kind"): i for i in r["result"]["issues"]}
    assert set(hits) == {"literal", "dynamic"}
    assert hits["literal"]["line"] == 1 and hits["dynamic"]["line"] == 2


def test_py_comments_ignored(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("# eval(x)\n# exec(y)\nx = 1\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert r["result"]["total"] == 0


def test_py_secret_literal_masked(tmp_path):
    key = "sk-" + "a" * 30
    f = tmp_path / "s.py"
    f.write_text(f'API_KEY = "{key}"\n', encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    iss = r["result"]["issues"]
    assert len(iss) == 1 and iss[0]["rule"] == "secret_literal"
    dumped = str(r)
    assert key not in dumped and "***len=" in dumped


def test_py_syntax_error_reported_not_crash(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    rules = _rules(r["result"])
    assert rules == ["syntax_error"]


# ---------- 分层声明：掩码状态机单测 ----------

def test_mask_preserves_length():
    src = 'a = `x ${y} z`; b = "s"; // c\n'
    masked, st = asx._mask_js(src)
    assert len(masked) == len(src)
    assert "${y}" in masked          # 插值代码保留
    assert '"s"' not in masked       # 字符串吞掉
    assert "// c" not in masked      # 注释吞掉


def test_node_modules_skipped_in_dir_mode(tmp_path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("eval(x)\n", encoding="utf-8")
    (tmp_path / "top.js").write_text("foo()\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(tmp_path)})
    assert r["result"]["files"] == 1
