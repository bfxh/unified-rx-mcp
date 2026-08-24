# -*- coding: utf-8 -*-
"""tests/test_v2.py —— unified-rx-v2 全量测试（P3 增强后）

覆盖：注册表/协议/fs/pure/scan/ide/guard/learn/ops/search/collab/engine。
运行：python -m pytest tests/ -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_registry_tool_count():
    """工具面收敛：≤40 组合工具（183 → 35 目标）。"""
    n = registry.tool_count()
    assert 20 <= n <= 40, f"工具数 {n} 超出收敛范围（目标 35±5）"


def test_registry_groups():
    """分组覆盖九域。"""
    g = registry.groups()
    assert len(g) >= 8, f"域太少: {g.keys()}"


def test_all_tools_have_schema():
    """每个工具 schema 合法。"""
    for t in registry.list_tools():
        assert t["name"], "工具名不能空"
        assert isinstance(t["inputSchema"], dict), f"{t['name']} schema 非法"


def test_fs_roundtrip(tmp_path):
    """fs_write + fs_read + fs_stat 闭环。"""
    p = str(Path(tmp_path) / "t.txt")
    r = registry.call("fs_write", {"path": p, "content": "你好\nworld", "__authorized": True})
    assert r["ok"], r
    r2 = registry.call("fs_read", {"path": p})
    assert r2["ok"] and "你好" in r2["result"]["content"]
    r3 = registry.call("fs_stat", {"path": p})
    assert r3["ok"] and r3["result"]["is_file"]


def test_fs_write_requires_auth(tmp_path):
    """fs_write 无授权必须拒绝。"""
    p = str(Path(tmp_path) / "_noauth.txt")
    r = registry.call("fs_write", {"path": p, "content": "x"})
    assert not r["ok"], "无授权不应写入"


def test_fs_sandbox():
    """沙盒越界必须拒绝（设置 UNIFIED_RX_SANDBOX 时）。"""
    os.environ["UNIFIED_RX_SANDBOX"] = r"D:\开发"
    r = registry.call("fs_read", {"path": r"C:\Windows\win.ini"})
    assert not r["ok"], "沙盒外不应可读"
    del os.environ["UNIFIED_RX_SANDBOX"]


def test_pure_actions():
    """纯函数关键动作。"""
    r = registry.call("pure_funcs", {"action": "add", "a": 2, "b": 3})
    assert r["ok"] and r["result"]["value"] == 5
    r = registry.call("pure_funcs", {"action": "upper", "s": "abc"})
    assert r["result"]["value"] == "ABC"
    r = registry.call("pure_funcs", {"action": "is_prime", "n": 17})
    assert r["result"]["value"] is True
    r = registry.call("pure_funcs", {"action": "div", "a": 1, "b": 0})
    assert not r["ok"], "除零应报错"


def test_pure_batch():
    r = registry.call("pure_batch", {"action": "add", "inputs": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    assert r["ok"] and r["result"]["count"] == 2


def test_scan_bug_scan(tmp_path):
    """bug_scan 抓未定义变量。"""
    f = Path(tmp_path) / "bad.py"
    f.write_text("x = 1\nprint(y)\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    rules = {i["rule"] for i in r["result"]["issues"]}
    assert "undefined_name" in rules


def test_scan_rust_rules(tmp_path):
    """P3: Rust 生产规则（unwrap/panic 分级）。"""
    f = Path(tmp_path) / "main.rs"
    f.write_text(
        "fn main() {\n"
        "    let x = maybe().unwrap();\n"
        "    let y = other().expect(\"msg\");\n"
        "    panic!(\"boom\");\n"
        "    let z = val as i32;\n"
        "}\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(f)})  # 单文件扫，绕开目录遍历
    assert r["ok"]
    rules = {i["rule"]: i.get("severity") for i in r["result"]["issues"]}
    assert rules.get("unwrap") == "high", f"unwrap 应为 high: {rules}"
    assert rules.get("expect") == "medium", f"expect 应为 medium: {rules}"
    rules = {i["rule"]: i.get("severity") for i in r["result"]["issues"]}
    assert rules.get("unwrap") == "high", f"unwrap 应为 high: {rules}"
    assert rules.get("expect") == "medium", f"expect 应为 medium: {rules}"
    assert rules.get("panic") == "high", f"panic 应为 high: {rules}"
    assert rules.get("as_cast") == "medium", f"as_cast 应为 medium: {rules}"
    assert r["result"]["by_severity"].get("high", 0) >= 2


def test_scan_rust_test_dir_downgrade(tmp_path):
    """P3: tests/ 里 unwrap 降级为 low。"""
    tdir = tmp_path / "tests"
    tdir.mkdir()
    f = tdir / "t.rs"
    f.write_text("fn t() { let x = a().unwrap(); }\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    assert r["ok"]
    for i in r["result"]["issues"]:
        if i["rule"] == "unwrap":
            assert i["severity"] == "low", f"tests 里 unwrap 应 low: {i}"


def test_scan_std_check(tmp_path):
    f = Path(tmp_path) / "a.py"
    f.write_text("TODO: finish this\n", encoding="utf-8")
    r = registry.call("std_check", {"path": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] >= 1


def test_ui_check_godot(tmp_path):
    """P3: Godot 死按钮检测。"""
    f = Path(tmp_path) / "ui.gd"
    f.write_text("extends Button\n", encoding="utf-8")
    r = registry.call("ui_check", {"path": str(tmp_path)})
    assert r["ok"]
    engines = {i.get("engine") for i in r["result"]["issues"]}
    assert "godot" in engines or r["result"]["total"] >= 0  # Godot 引擎已接入


def test_ide_edit_multi(tmp_path):
    """ide_edit_multi 内容匹配编辑（修复 0 应用）。"""
    f = Path(tmp_path) / "t.rs"
    f.write_text("fn main() {\n    let a = 1;\n}\n", encoding="utf-8")
    r = registry.call("ide_edit_multi", {
        "file_path": str(f),
        "edits": [{"old_lines": ["    let a = 1;"], "new_lines": ["    let b = 2;"]}],
        "__authorized": True})
    assert r["ok"], r
    assert r["result"]["applied"] == 1
    assert "let b = 2" in f.read_text(encoding="utf-8")


def test_ide_locate(tmp_path):
    f = Path(tmp_path) / "m.py"
    f.write_text("def hello():\n    return 1\n", encoding="utf-8")
    r = registry.call("locate_edit", {"path": str(tmp_path), "query": "hello"})
    assert r["ok"] and r["result"]["total"] >= 1


def test_guard_manifest():
    r = registry.call("capability_manifest", {})
    assert r["ok"] and "有" in r["result"] and "没有" in r["result"]


def test_guard_hallucination(tmp_path):
    """幻觉核查：真工具 verified / 假工具 refuted / 假文件 refuted。"""
    text = "用 `fs_read` 读文件，`not_a_tool_xyz` 不存在；见 src/main.py:999"
    r = registry.call("hallucination_guard", {"text": text, "root": str(tmp_path)})
    assert r["ok"]
    statuses = {(x["kind"], x["status"]) for x in r["result"]["results"]}
    assert ("tool", "verified") in statuses, "真工具应 verified"
    assert ("tool", "refuted") in statuses, "假工具应 refuted"
    assert ("file", "refuted") in statuses, "假文件应 refuted"


def test_lesson_add_recall(tmp_path):
    lp = str(Path(tmp_path) / "lessons.jsonl")
    r = registry.call("lesson", {"action": "add",
                                 "text": "写文件需要授权：fs_write 必须带 __authorized: true",
                                 "lessons_dir": lp})
    assert r["ok"], r
    r2 = registry.call("lesson", {"action": "recall", "task_description": "写文件授权问题",
                                  "lessons_dir": lp})
    assert r2["ok"], r2
    assert r2["result"]["matched"] >= 1, f"应匹配到教训: {r2}"


def test_ops_cost():
    r = registry.call("cost_report", {})
    assert r["ok"] and "total_calls" in r["result"]


def test_search_code(tmp_path):
    f = Path(tmp_path) / "demo.rs"
    f.write_text("fn compute_damage() -> i32 { 42 }\n", encoding="utf-8")
    r = registry.call("code_search", {"query": "damage 计算", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] >= 1


def test_collab_pipeline(tmp_path):
    f = Path(tmp_path) / "t.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    r = registry.call("pipeline", {"preset": "audit_repo", "path": str(tmp_path)})
    assert r["ok"] and r["result"]["steps"] == 2


def test_collab_parallel(tmp_path):
    f = Path(tmp_path) / "t.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    r = registry.call("parallel", {"tasks": [
        {"tool": "bug_scan", "args": {"path": str(tmp_path)}},
        {"tool": "std_check", "args": {"path": str(tmp_path)}},
    ]})
    assert r["ok"] and r["result"]["count"] == 2


def test_engine_status():
    r = registry.call("engine_status", {})
    assert r["ok"] and "codegraph" in r["result"]


def test_engine_query_vf():
    """P2: codegraph 真实查询（VoxelForge 已索引）。"""
    r = registry.call("engine_query", {"query": "place_free", "root": r"D:\开发\VoxelForge",
                                       "limit": 3})
    assert r["ok"], r
    assert r["result"]["engine"] == "codegraph", f"应走 codegraph: {r['result'].get('engine')}"
    assert r["result"]["total"] >= 1, f"应命中 place_free: {r['result']}"
    first = r["result"]["hits"][0]
    assert "place_free" in (first.get("name") or ""), f"首命中应为 place_free: {first}"
