# -*- coding: utf-8 -*-
"""S83：bug_scan 全量 Rust 原生化（rx-scan bugscan 子命令）的契约测试。

Python 侧退化为薄壳转调（同 S82 std_check 模式）：registry 契约、exe 缺失
清晰报错、路径不存在结构化错误、位置参数签名、旧 Python 实现退役断言。
语义等价由 S83 对照实验承担（$TEMP/s83_oracle.py 7 场景逐字节一致）；行为
回归由 test_v2.py / test_bevy.py / test_r1_guards.py 原样继续；解析器边界
语义由 rust/tests/bug_test.rs 承担。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401
import tools.scan as tscan


def test_bug_scan_registry_contract(tmp_path):
    """bug_scan 经 registry 走 exe：结果契约（files/total/by_rule/by_severity/issues）不变。"""
    (tmp_path / "a.py").write_text(
        "from os import id\ntry:\n    pass\nexcept:\n    pass\n"
        "def f(unused_arg):\n    print(undefined_var)\n", encoding="utf-8")
    (tmp_path / "b.rs").write_text(
        "fn main() {\n    let x = maybe().unwrap();\n    panic!(\"boom\");\n}\n",
        encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"], r
    res = r["result"]
    assert res["files"] == 2, res
    assert res["total"] >= 3 and res["total"] == len(res["issues"]), res
    assert {"file", "line", "rule", "msg"} <= set(res["issues"][0])
    rules = {i["rule"] for i in res["issues"]}
    assert {"bare_except", "undefined_name", "redefined_import", "unwrap", "panic"} <= rules, rules
    assert sum(res["by_severity"].values()) == res["total"]


def test_bug_scan_path_not_exist_structured_error(tmp_path):
    r = registry.call("bug_scan", {"path": str(tmp_path / "nope")})
    assert not r["ok"] and "路径不存在" in r["error"], r


def test_bug_scan_exe_missing_clear_error(tmp_path, monkeypatch):
    """exe 缺失必须清晰报错，不静默降级（S79 既定政策）。"""
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", r"Z:\nope\rx-scan.exe")
    monkeypatch.setenv("TEMP", str(tmp_path))
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert not r["ok"] and "rx-scan.exe 不存在" in r["error"], r


def test_bug_scan_positional_call(tmp_path):
    """薄壳保持位置参数签名 (path, max_files)，返回裸结果 dict（非包络）。"""
    (tmp_path / "a.py").write_text("v = missing_z\n", encoding="utf-8")
    r = tscan.bug_scan(str(tmp_path), 10)
    assert r["files"] == 1 and r["total"] == 1, r
    assert r["issues"][0]["rule"] == "undefined_name", r


def test_bug_scan_schema_contract():
    assert registry._TOOLS["bug_scan"]["group"] == "scan"
    assert registry._TOOLS["bug_scan"]["schema"]["required"] == ["path"]
    assert set(registry._TOOLS["bug_scan"]["schema"]["properties"]) == {"path", "max_files"}


def test_bug_scan_python_semantics_via_exe(tmp_path):
    """薄壳下抽查 Python 语义规则：语法错误/裸 except/动态执行 severity 词表。"""
    (tmp_path / "syn.py").write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "dyn.py").write_text("eval('1+1')\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"], r
    by_rule = {}
    for i in r["result"]["issues"]:
        by_rule.setdefault(i["rule"], []).append(i)
    assert "syntax_error" in by_rule, by_rule
    ev = by_rule["eval_exec"][0]
    assert ev["severity"] == "high" and ev["kind"] == "definite", ev
    assert "裸调用" in ev["msg"], ev
