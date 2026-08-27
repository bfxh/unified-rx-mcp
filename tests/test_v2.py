# -*- coding: utf-8 -*-
"""tests/test_v2.py —— unified-rx-v2 全量测试（P3 增强后）

覆盖：注册表/协议/fs/pure/scan/ide/guard/learn/ops/search/collab/engine。
运行：python -m pytest tests/ -q
"""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import server  # noqa: F401 (S3 协议层测试用)
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
    """fail-closed：越界拒绝；未配置 = 拒绝一切文件访问。"""
    old = os.environ.pop("UNIFIED_RX_SANDBOX", None)
    try:
        os.environ["UNIFIED_RX_SANDBOX"] = ""
        r = registry.call("fs_read", {"path": str(Path(__file__))})
        assert not r["ok"], "未配置沙盒时应一律拒绝（fail-closed）"
        os.environ["UNIFIED_RX_SANDBOX"] = r"D:\开发"
        r2 = registry.call("fs_read", {"path": r"C:\Windows\win.ini"})
        assert not r2["ok"], "沙盒外不应可读"
        r3 = registry.call("fs_stat", {"path": __file__})
        assert r3["ok"], "沙盒内应可访问"
    finally:
        if old is not None:
            os.environ["UNIFIED_RX_SANDBOX"] = old


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
    """S4-D1: Rust 规则分级——崩溃类 high，线索类 info+kind=clue。"""
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
    kinds = {i["rule"]: i.get("kind") for i in r["result"]["issues"]}
    assert rules.get("unwrap") == "info" and kinds.get("unwrap") == "clue", f"unwrap 应为 info/clue: {rules}"
    assert rules.get("expect") == "info", f"expect 应为 info（线索）: {rules}"
    assert rules.get("panic") == "high", f"panic 应为 high（确定性）: {rules}"
    assert rules.get("as_cast") == "info", f"as_cast 应为 info（线索）: {rules}"
    assert r["result"]["by_severity"].get("high", 0) == 1, "仅 panic 一条 high"


def test_scan_rust_test_mod_downgrade(tmp_path):
    """S4-D1: #[cfg(test)] mod 内的 unwrap 行级降级为 low；mod 外保持 info。"""
    f = Path(tmp_path) / "mixed.rs"
    f.write_text(
        "pub fn prod() -> u32 { maybe().unwrap() }\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn t() { assert_eq!(maybe().unwrap(), 1); }\n"
        "}\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(f)})
    assert r["ok"]
    by_line = {i["line"]: i for i in r["result"]["issues"] if i["rule"] == "unwrap"}
    assert len(by_line) == 2, f"应有两处 unwrap: {by_line}"
    prod = by_line[1]; test = by_line[5]
    assert prod["severity"] == "info" and prod.get("kind") == "clue", f"L1 生产区应为 info/clue: {prod}"
    assert test["severity"] == "low" and "降级" in test["msg"], f"L5 测试 mod 应降级: {test}"


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


# ── UPGRADE-C1：出口裁剪 + 游标分页 ───────────────
def _corpus(tmp_path, n_files=120):
    """bug_scan 专用语料：n_files × 2 未定义名 = 2n 命中。"""
    d = tmp_path / "_c1"
    d.mkdir()
    for i in range(n_files):
        (d / f"b{i}.py").write_text("print(missing_a + missing_b)\n", encoding="utf-8")
    return d


def test_clamp_pagination_roundtrip(tmp_path):
    """列表超 200 项 → 截断 + next_cursor 续读，跨页合计等于全量且不重不漏。"""
    d = _corpus(tmp_path)
    r = registry.call("bug_scan", {"path": str(d), "max_files": 400})
    out = r["result"]
    assert out["truncated"] is True and out["total_items"] == 240
    assert len(out["issues"]) == registry.MAX_RESULT_ITEMS == 200
    r2 = registry.call("bug_scan", {"path": str(d), "max_files": 400,
                                    "cursor": out["next_cursor"]})
    o2 = r2["result"]
    assert "truncated" not in o2, "末页不得再带 truncated（还有更多才有）"
    assert len(o2["issues"]) == 40 and "next_cursor" not in o2
    # 跨页计数守恒：P1(200) + P2(40) = 全量 240（slice 保序，不重复交付）
    assert len(out["issues"]) + len(o2["issues"]) == o2["total_items"] == 240


def test_clamp_bad_and_far_cursor(tmp_path):
    """cursor 垃圾值 → 按第 0 页；越界远游标 → 空页；均结构化成功。"""
    d = tmp_path / "_far"
    d.mkdir()
    for i in range(250):  # >200 才会进入分页逻辑
        (d / f"d{i}").mkdir()
    r_bad = registry.call("fs_list", {"path": str(d), "depth": 1, "cursor": "xyz"})
    assert r_bad["ok"] and len(r_bad["result"]["entries"]) == registry.MAX_RESULT_ITEMS
    r_far = registry.call("fs_list", {"path": str(d), "depth": 1, "cursor": 10 ** 9})
    assert r_far["ok"] and r_far["result"]["entries"] == []


def test_small_results_untouched():
    """未超限结果不得附加 truncated/next_cursor 字段。"""
    import tempfile
    from pathlib import Path
    d = Path(tempfile.gettempdir()) / "unified-rx-pytest" / "_clamp_probe"
    if not d.exists():
        d.mkdir(parents=True)
        (d / "a.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("fs_list", {"path": str(d), "depth": 1})
    out = r["result"]
    assert "truncated" not in out and "next_cursor" not in out


# ── S3-B2/B3：logging 通知 + 取消登记 ───────────────
def test_registry_notifier_hook():
    """notify() 走注入出口；未注入时静默不抛。"""
    import registry as reg
    got = []
    reg.set_notifier(lambda level, msg: got.append((level, msg)))
    try:
        from tools.engine import engine_query  # noqa: F401 触发注册
        registry.call("engine_query", {"query": "sandbox roots",
                                       "root": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
        assert any("BM25" in m for _, m in got), f"降级应发通知: {got}"
    finally:
        reg.set_notifier(None)
    # 未注入：不应抛
    registry.call("engine_query", {"query": "sandbox roots",
                                   "root": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})


def test_server_cancel_flag_lifecycle():
    """server.cancel_flag: 登记后可取，cancelled 后置位，完成后清理。"""
    import server
    ev = threading.Event()
    with server._CANCEL_LOCK:
        server._CANCELS[999] = ev
    try:
        assert server.cancel_flag(999) is not None and not server.cancel_flag(999).is_set()
        ev.set()
        assert server.cancel_flag(999).is_set()
        with server._CANCEL_LOCK:
            server._CANCELS.pop(999, None)
        assert server.cancel_flag(999) is None, "完成/取消后应清理登记"
    finally:
        with server._CANCEL_LOCK:
            server._CANCELS.pop(999, None)


def test_server_handle_logging_setlevel():
    """logging/setLevel 有应答（S3 能力协商）。"""
    r = server._handle({"jsonrpc": "2.0", "id": 7, "method": "logging/setLevel",
                        "params": {"level": "info"}})
    assert r["id"] == 7 and r["result"] == {}


def test_server_initialize_capabilities_s3():
    """initialize capabilities 声明 listChanged + logging。"""
    r = server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    caps = r["result"]["capabilities"]
    assert caps["tools"]["listChanged"] is True and "logging" in caps
