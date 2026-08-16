# -*- coding: utf-8 -*-
"""causal_debug + differentiable_code 测试（阶段1/2）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_causal_link_record(tmp_path, monkeypatch):
    """因果链记录（cause→effect 入 scan-log——行为链可查）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    repo = tmp_path / "repo"
    repo.mkdir()
    d = json.loads(server._call("causal", {"action": "link",
        "root": str(repo), "effect": "构建失败",
        "cause": "Agent A 改了 placement.rs 的 scale"})[0].text)
    assert d["ok"] is True, d
    import scan_log_core
    logs = scan_log_core.query_logs(limit=10)
    links = [l for l in logs if l.get("tool") == "causal_link"]
    assert links and "Agent A" in links[0]["summary"], links


def test_causal_trace_chain(tmp_path, monkeypatch):
    """因果溯源：失败关键词 → 候选原因链（工具调用因果）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    repo = tmp_path / "repo"
    repo.mkdir()
    import scan_log_core
    scan_log_core.append_scan({"tool": "bug_scan", "root": str(repo),
                               "ok": False, "summary": "检测到 3 个 fail 问题"})
    d = json.loads(server._call("causal", {"action": "trace",
        "root": str(repo), "fail_keyword": "fail"})[0].text)
    assert d["ok"] is True, d
    assert d["fail_events"], f"应定位失败事件: {d}"
    assert d["causal_chain"], f"应有因果链: {d}"
    assert "bug_bisect" in d["advice"], d


def test_bug_bisect_plan(tmp_path, monkeypatch):
    """git bisect 二分计划（只读——不 checkout）。"""
    from causal_debug import bug_bisect
    # 非 git 目录 → 诚实报错
    repo = tmp_path / "repo"
    repo.mkdir()
    d = bug_bisect(str(repo), "abc123", "HEAD", "cargo test")
    assert d["ok"] is False, f"非 git 应报错: {d}"


def test_optimize_code_complexity(tmp_path, monkeypatch):
    """性能目标驱动优化：嵌套循环/同步 IO 检出。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "hot.py"
    f.write_text(
        "def process(items):\n"
        "    for a in items:\n"
        "        for b in items:\n"
        "            print(a + b)\n"
        "    f = open('x.txt')\n",
        encoding="utf-8")
    d = json.loads(server._call("optimize_code", {
        "path": str(f), "perf_goal": "响应时间<10ms"})[0].text)
    assert d["ok"] is True, d
    kinds = {x["kind"] for x in d["findings"]}
    assert "complexity" in kinds, f"嵌套循环应检出: {kinds}"
    assert "io_in_hot_path" in kinds, f"同步 IO 应检出: {kinds}"
    assert "真·AST 梯度下降重写为未来方向" in d["note"], d


def test_code_embed_similarity(tmp_path, monkeypatch):
    """AST 符号嵌入：相似函数检索（结构特征）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def add(x, y):\n    return x + y\n", encoding="utf-8")
    b.write_text("def sub(x, y):\n    return x - y\n", encoding="utf-8")
    d = json.loads(server._call("code_embed", {
        "path": str(a), "compare": str(b)})[0].text)
    assert d["ok"] is True, d
    assert d["top"], f"应有相似结果: {d}"
    # 单文件嵌入清单
    d2 = json.loads(server._call("code_embed", {"path": str(a)})[0].text)
    assert d2["ok"] is True and d2["count"] == 1, d2
    assert d2["functions"][0]["features"]["name"] == "add", d2


def test_bug_bisect_execute_flag(monkeypatch):
    """execute=True → git bisect 原生路径（非 git 目录诚实报错——不崩溃）。"""
    from causal_debug import bug_bisect
    import tempfile
    repo = tempfile.mkdtemp(prefix="bisect_ex_")  # 系统临时目录（非 git 仓库内）
    d = bug_bisect(repo, "abc123", "HEAD", "cargo test", execute=True)
    assert d["ok"] is False, f"非 git 目录 execute 应报错: {d}"
    assert "bisect" in d.get("error", ""), d


def test_bug_bisect_extract_first_bad():
    """first bad commit 提取（git bisect run 输出解析）。"""
    from causal_debug import _extract_first_bad
    out = "Bisecting: 5 revisions left\nfirst bad commit: [a1b2c3d4] 引入 bug 的提交\n"
    assert _extract_first_bad(out) == "a1b2c3d4", _extract_first_bad(out)
    assert _extract_first_bad("no bad commit here") is None


def test_optimize_code_rewrites(tmp_path, monkeypatch):
    """optimize_code 附等价重写片段（热点→可直接应用代码）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "hot2.py"
    f.write_text("def p(items):\n    for a in items:\n"
                 "        for b in items:\n            print(a)\n"
                 "    f = open('x')\n", encoding="utf-8")
    d = json.loads(server._call("optimize_code", {
        "path": str(f), "perf_goal": "响应时间<10ms"})[0].text)
    assert d["ok"] is True, d
    rewrites = d.get("rewrites") or []
    kinds = {r["kind"] for r in rewrites}
    assert "complexity" in kinds, f"应有复杂度重写片段: {kinds}"
    assert "io_in_hot_path" in kinds, f"应有 IO 重写片段: {kinds}"
