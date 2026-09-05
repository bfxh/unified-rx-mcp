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


# ---------- Rust 结构化层（VoxelForge 主语言） ----------

def test_rust_comment_and_string_never_flagged(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text('// unwrap() in comment\nlet s = "expect(x)";\n', encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert r["result"]["total"] == 0, r


def test_rust_unwrap_flagged_outside_test(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text("fn load() {\n    let v = x.unwrap();\n}\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert any(i["rule"] == "rust_unwrap_expect" for i in r["result"]["issues"])


def test_rust_cfg_test_module_suppressed(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text("#[cfg(test)]\nmod tests {\n    fn it() { let v = x.unwrap(); }\n}\n",
                 encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert not [i for i in r["result"]["issues"] if i["rule"] == "rust_unwrap_expect"], r


def test_rust_unsafe_block_detected_with_fn_inventory(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text("fn spawn() {\n    unsafe { raw_ptr() }\n}\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    res = r["result"]
    assert any(i["rule"] == "rust_unsafe" for i in res["issues"])
    assert res["units"][0]["fn_count"] == 1 and res["units"][0]["unsafe_count"] == 1


def test_rust_lifetime_not_masked_as_char(tmp_path):
    """生命周期 'a 不破坏后续解析：unsafe 仍被检出。"""
    f = tmp_path / "t.rs"
    f.write_text("fn foo<'a>(x: &'a str) -> &'a str {\n    unsafe { x }\n}\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    assert any(i["rule"] == "rust_unsafe" for i in r["result"]["issues"])


# ---------- S12 函数级切片归属 ----------

def test_rust_fn_attribution_and_risky_fns(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text(
        "fn clean() { let a = 1; }\n"
        "fn dirty() {\n    x.unwrap();\n    y.unwrap();\n}\n"
        "fn tricky() {\n    unsafe { z() }\n}\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(f)})
    res = r["result"]
    owners = {(i["rule"], i["fn"]) for i in res["issues"]}
    assert ("rust_unwrap_expect", "dirty") in owners
    assert ("rust_unsafe", "tricky") in owners
    risky = {d["fn"]: d for d in res["units"][0]["risky_fns"]}
    assert risky["dirty"]["unwrap"] == 2
    assert "clean" not in risky


# ---------- 分层声明：S84 起掩码状态机在 rust/src/astscan.rs（cargo test 覆盖） ----------

def test_old_python_internals_retired():
    """S84 前的纯 Python 实现已退役：内部函数/正则不得复活（薄壳是唯一路径）。"""
    for name in ("_JS_SINKS_BARE", "_PY_SINKS_NAME", "_PY_ATTR_SHELL", "_SECRET_SHAPE",
                 "_scan_python_ast", "_mask_js", "_CALL_RE", "_scan_js_calls",
                 "_PANIC_CALL_RE", "_SAFE_IDENT", "_MOD_TEST_ATTR", "_mask_rust",
                 "_scan_rust_struct", "_RUST_IDENT_RE", "_RUST_KEYWORDS",
                 "_rust_defs_and_refs", "rust_reach"):
        assert not hasattr(asx, name), name
    # 薄壳三件套必须保留
    for name in ("_rx_scan_exe", "_rx_scan_call", "ast_scan"):
        assert hasattr(asx, name), name


def test_exe_missing_raises_clear_error(tmp_path, monkeypatch):
    """exe 缺失报清晰错误不静默降级：ValueError 指向 cargo build 与 env 覆盖。"""
    monkeypatch.delenv("UNIFIED_RX_RS_EXE", raising=False)
    monkeypatch.setenv("TEMP", str(tmp_path))   # 候选路径全部落空（真 TEMP 里有现成 exe）
    with pytest.raises(ValueError, match="rx-scan\\.exe 不存在"):
        asx._rx_scan_call(str(tmp_path), 5)


def test_node_modules_skipped_in_dir_mode(tmp_path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("eval(x)\n", encoding="utf-8")
    (tmp_path / "top.js").write_text("foo()\n", encoding="utf-8")
    r = registry.call("ast_scan", {"path": str(tmp_path)})
    assert r["result"]["files"] == 1


# ── S16：Rust 跨文件引用可达性 ───────────────
def _reach_call(tmp_path):
    """跑目录级 ast_scan，返回 (issues, rust_reach)。"""
    r = registry.call("ast_scan", {"path": str(tmp_path)})
    assert r["ok"], r
    return r["result"]["issues"], r["result"]["rust_reach"]


def test_reach_test_only_helper_flagged(tmp_path):
    """src 辅助函数只被 tests/ 引用 → test_only；其 unwrap 打上 reach 标记。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "helper.rs").write_text(
        "pub fn calc_bonus() -> u32 {\n"
        "    let x = maybe().unwrap();\n"
        "    x + 1\n"
        "}\n", encoding="utf-8")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "t.rs").write_text("mod it { use super::*; #[test] fn a() { assert!(calc_bonus() >= 0); } }\n",
                               encoding="utf-8")
    issues, reach = _reach_call(tmp_path)
    u = [i for i in issues if i["rule"] == "rust_unwrap_expect"]
    assert u and all(i["fn"] == "calc_bonus" and i.get("reach") == "test_only" for i in u), u
    names = {h["fn"] for h in reach["test_only_helpers"]}
    assert "calc_bonus" in names, reach


def test_reach_bare_ident_registration_counts_as_prod(tmp_path):
    """bevy 风格 add_systems(Tick, system_fn) 的裸标识符注册 = 生产引用，不降级。"""
    f = tmp_path / "sys.rs"
    f.write_text(
        "pub fn system_fn(mut q: Query<&mut Pos>) { let p = q.single_mut().unwrap(); }\n"
        "pub fn setup(app: &mut App) { app.add_systems(Update, system_fn); }\n", encoding="utf-8")
    issues, reach = _reach_call(tmp_path)
    u = [i for i in issues if i["rule"] == "rust_unwrap_expect"]
    assert u and all(i["fn"] == "system_fn" and i["reach"] == "prod" for i in u), u


def test_reach_unreferenced_is_signal_not_downgrade(tmp_path):
    """零引用函数只标 unreferenced（死代码信号）；issue 保留 reach 字段与计数。"""
    f = tmp_path / "dead.rs"
    f.write_text('fn orphan_helper() -> u32 { maybe().expect("x") }\n', encoding="utf-8")
    issues, reach = _reach_call(tmp_path)
    u = [i for i in issues if i["rule"] == "rust_unwrap_expect"]
    assert u and u[0]["fn"] == "orphan_helper" and u[0]["reach"] == "unreferenced", u
    assert reach["by_reach"]["unreferenced"] >= 1


def test_reach_no_rs_input_returns_none(tmp_path):
    (tmp_path / "a.py").write_text("eval('1+1')\n", encoding="utf-8")
    _, reach = _reach_call(tmp_path)
    assert reach is None


def test_reach_same_name_two_files_distinct_verdicts(tmp_path):
    """同名 fn 分属 prod/test 文件：prod 定义参与归类，test 定义跳过。"""
    s = tmp_path / "src"
    s.mkdir()
    (s / "lib.rs").write_text(
        "fn shared() -> u32 { 1 }\n"
        "pub fn caller_prod() -> u32 { shared() + shared() }\n", encoding="utf-8")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "t2.rs").write_text("#[test]\nfn use_shared() { shared(); }\n", encoding="utf-8")
    issues, reach = _reach_call(tmp_path)
    # shared 在 src 有定义且被 prod(caller_prod) 与 test 双方引用 → prod
    sh = [e["reach"] for e in reach.get("entries", []) if e["fn"] == "shared"]
    assert any(r == "prod" for r in sh), sh
