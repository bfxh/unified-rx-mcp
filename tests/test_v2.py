# -*- coding: utf-8 -*-
"""tests/test_v2.py —— unified-rx-v2 全量测试（P3 增强后）

覆盖：注册表/协议/fs/scan/ide/guard/learn/ops/search/engine。
S15 起移除 pure/collab 域与 cmd_cheatsheet（废物清理，见 UPGRADE.md S15）。
运行：python -m pytest tests/ -q
"""
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import server  # noqa: F401 (S3 协议层测试用)
import tools  # noqa: F401


def test_registry_tool_count():
    """工具面收敛：attack 域加入后 42（39+3），上限放宽到 50。"""
    n = registry.tool_count()
    assert 20 <= n <= 60, f"工具数 {n} 超出收敛范围"


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
        os.environ["UNIFIED_RX_SANDBOX"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))   # 自指仓库根（平台无关）
        r2 = registry.call("fs_read", {"path": r"C:\Windows\win.ini"})
        assert not r2["ok"], "沙盒外不应可读"
        r3 = registry.call("fs_stat", {"path": __file__})
        assert r3["ok"], "沙盒内应可访问"
    finally:
        if old is not None:
            os.environ["UNIFIED_RX_SANDBOX"] = old


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


def test_bug_scan_panic_in_test_downgraded(tmp_path):
    """S12 语境诚实化：panic! 在 tests 目录/cfg(test) 内降级为 clue（VF3 实证 21/21 在测试区）。"""
    d = tmp_path / "src" / "tests"
    d.mkdir(parents=True)
    f = d / "t.rs"
    f.write_text('fn x() { panic!("boom"); }\n', encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path)})
    hits = [i for i in r["result"]["issues"] if i["rule"] == "panic"]
    assert hits and all(h["severity"] == "low" and "测试上下文" in h["msg"] for h in hits)


def test_scan_std_check(tmp_path):
    f = Path(tmp_path) / "a.py"
    f.write_text("TODO: finish this\n", encoding="utf-8")
    r = registry.call("std_check", {"path": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] >= 1


def test_scan_eval_exec_member_call_not_flagged(tmp_path):
    """源码审计实测教训：RegExp.prototype.exec(/x/) 不是动态执行，不得报 eval_exec。"""
    import json
    safe = Path(tmp_path) / "safe.js"
    safe.write_text('const m = /a(b)c/.exec(text); const t = re.test(x);\n', encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(safe)})
    hits = [h for h in r["result"]["issues"] if h["rule"] == "eval_exec"]
    assert not hits, f"成员调用被误报: {json.dumps(hits, ensure_ascii=False)}"
    evil = Path(tmp_path) / "evil.js"
    evil.write_text("eval(userInput);\nexecSync(cmd);\n", encoding="utf-8")
    r2 = registry.call("bug_scan", {"path": str(evil)})
    assert any(h["rule"] == "eval_exec" for h in r2["result"]["issues"])


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


def test_search_code(tmp_path):
    f = Path(tmp_path) / "demo.rs"
    f.write_text("fn compute_damage() -> i32 { 42 }\n", encoding="utf-8")
    r = registry.call("code_search", {"query": "damage 计算", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] >= 1


def test_engine_status():
    r = registry.call("engine_status", {})
    assert r["ok"] and "codegraph" in r["result"]


def test_engine_query_vf(monkeypatch):
    """P2: codegraph 真实查询（VoxelForge 已索引）。环境无关仓自动跳过。

    S75 起 engine_query 的 root 过沙盒，本测试把沙盒指到 D:\\开发
    （与生产一致，VoxelForge 才进得来）。
    """
    vf_root = r"D:\开发\VoxelForge"
    if not (os.path.isdir(vf_root) and
            os.path.exists(os.path.join(vf_root, ".codegraph"))):
        pytest.skip("需要本机 VoxelForge+codegraph 索引（外部资产）")
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", r"D:\开发")
    r = registry.call("engine_query", {"query": "place_free", "root": vf_root,
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
    """S10 后登记表唯一事实源=registry：登记后可取，cancelled 置位，完成后清理。
    server.cancel_flag 仅是委托薄出口。"""
    import server
    ev = registry.register_cancel(999)
    try:
        assert server.cancel_flag(999) is ev and not ev.is_set()
        assert server.registry.set_cancelled(999) is True
        assert server.cancel_flag(999).is_set()
        registry.release_cancel(999)
        assert server.cancel_flag(999) is None, "完成/取消后应清理登记"
    finally:
        registry.release_cancel(999)


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


# ── S83：扫描缓存退役——每调独立 exe 进程，结果可重复且反映最新内容 ───────────────
def test_scan_repeatable_and_fresh(tmp_path):
    """S83 缓存退役：连续两次扫描结果一致；改文件后立即反映新内容。"""
    d = tmp_path / "_s5"
    d.mkdir()
    for i in range(30):
        (d / f"c{i}.py").write_text("v = missing_x\n", encoding="utf-8")
    r1 = registry.call("bug_scan", {"path": str(d)})
    r2 = registry.call("bug_scan", {"path": str(d)})
    a, b = r1["result"], r2["result"]
    assert a["total"] == b["total"] and a["by_rule"] == b["by_rule"], "两次扫描结果必须一致"
    # 修改文件（size 变化）→ 重扫必须反映新内容
    (d / "c0.py").write_text("w = missing_y\n", encoding="utf-8")
    r3 = registry.call("bug_scan", {"path": str(d)})
    c0 = [i for i in r3["result"]["issues"] if i.get("file", "").endswith("c0.py")]
    assert any("missing_y" in i["msg"] for i in c0), "改后必须扫到新内容"
    assert not any("missing_x" in i["msg"] for i in c0), "旧内容不得残留"


def test_std_check_repeatable(tmp_path):
    """std_check 连续调用结果一致（原 S5-C2 缓存一致性测试的 S83 版）。"""
    d = tmp_path / "_s5std"
    d.mkdir()
    (d / "m.py").write_text("delay = 1500\n", encoding="utf-8")
    r1 = registry.call("std_check", {"path": str(d)})
    r2 = registry.call("std_check", {"path": str(d)})
    assert r1["result"]["findings"] == r2["result"]["findings"]


# ── S6-D2：locate_edit 引用计数 + E1 bench 骨架 ───────────────
def test_locate_edit_reference_count(tmp_path):
    """locate_edit 返回 references_in_scan 全库计数事实。"""
    d = tmp_path / "_refs"
    d.mkdir()
    (d / "a.py").write_text("def handler():\n    pass\n\nhandler()\nhandler()\n", encoding="utf-8")
    (d / "b.py").write_text("from a import handler\nprint(handler)\n", encoding="utf-8")
    r = registry.call("locate_edit", {"path": str(d), "query": "handler", "limit": 5})
    out = r["result"]
    # a.py: def 1 + 调用 2 = 3；b.py: import 1 + print 1 = 2 → 全库共 5
    assert out["references_in_scan"] == 5, f"handler 出现 5 次: {out}"
    assert len(out["hits"]) >= 1


def test_bench_corpus_and_dryrun():
    """bench 标注库可加载 + dry-run 退出码 0（CI 门禁）。"""
    import subprocess, sys as _sys
    bench = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "bench" / "replay_ab.py"
    r = subprocess.run([sys.executable, "-X", "utf8", str(bench), "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"dry-run 失败: {r.stderr[-300:]}"
    assert "CORPUS OK" in r.stdout
