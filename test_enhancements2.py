"""repo_wiki / communities / multi_agent / codeql-angr 回归测试（P4 收官）。

与 test_enhancements.py 互补：覆盖本轮新增模块的核心行为。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multi_agent import orchestrate, role_catalog, ROLE_TOOLS


def _tmp(name):
    d = os.path.join(tempfile.gettempdir(), f"hermes_pytest_{name}")
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    return d


def _clean(d):
    shutil.rmtree(d, ignore_errors=True)


# ── repo_wiki ─────────────────────────────────────────────
def test_repo_wiki_generates_markdown():
    from repo_wiki import generate_wiki
    d = _tmp("wiki")
    src = os.path.join(d, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "m.py"), "w", encoding="utf-8") as fh:
        fh.write("def helper():\n    return 1\n\ndef main():\n    return helper()\n")
    out = os.path.join(d, "WIKI.md")
    r = generate_wiki(src, out)
    assert r["ok"] is True
    md = open(out, encoding="utf-8").read()
    assert "代码库 Wiki" in md
    assert "模块地图" in md
    assert "helper" in md  # 核心符号/模块清单含符号
    _clean(d)


def test_repo_wiki_via_tool():
    import server
    d = _tmp("wiki2")
    with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("def f1():\n    return 1\n")
    r = server._call("repo_wiki", {"root": d})
    parsed = json.loads(r[0].text)
    assert parsed["ok"] is True
    assert os.path.exists(parsed["wiki"])
    _clean(d)


# ── communities ───────────────────────────────────────────
def test_communities_detection():
    from graph_index import GraphIndex
    d = _tmp("comm")
    src = os.path.join(d, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("def fa():\n    return fb()\n\ndef fb():\n    return 1\n")
    with open(os.path.join(src, "b.py"), "w", encoding="utf-8") as fh:
        fh.write("def fc():\n    return fa()\n")
    gi = GraphIndex(os.path.join(d, "g.db"))
    gi.index_directory(src)
    comms = gi.communities()
    assert isinstance(comms, list)
    assert len(comms) >= 1
    assert comms[0]["size"] >= 1
    assert 0.0 <= comms[0]["density"] <= 1.0  # 修复后密度必须 ≤1
    _clean(d)


# ── multi_agent ───────────────────────────────────────────
def test_orchestrate_parallel():
    def call_fn(tool, args):
        return {"tool": tool, "echo": args.get("x")}
    tasks = [
        {"id": "t1", "role": "analyst", "tool": "repo_graph", "args": {"x": 1}},
        {"id": "t2", "role": "analyst", "tool": "kb_query", "args": {"x": 2}},
    ]
    r = orchestrate(tasks, call_fn)
    assert r["ok"] is True
    assert r["stats"]["succeeded"] == 2
    assert r["results"]["t1"]["result"]["echo"] == 1


def test_orchestrate_rejects_privilege_escalation():
    def call_fn(tool, args):
        return {}
    tasks = [{"id": "x", "role": "writer", "tool": "quality_scan", "args": {}}]
    r = orchestrate(tasks, call_fn)
    assert r["ok"] is False
    assert any("不属于" in e.get("error", "") for e in r["errors"].values())


def test_role_catalog_complete():
    cats = role_catalog()
    assert set(cats) == {"analyst", "quality", "memory", "writer", "explorer"}
    assert "repo_graph" in cats["analyst"]["tools"]
    assert "quality_scan" in cats["quality"]["tools"]


# ── quality_engine 顶级后端探测 ───────────────────────────
def test_codeql_angr_detection():
    from quality_engine import QualityEngine
    qe = QualityEngine()
    avail = qe.available()
    assert "codeql" in avail
    assert "angr" in avail
    # 未安装时降级不炸
    if not avail["codeql"]:
        r = qe.codeql_check(os.getcwd())
        assert r["available"] is False
    if not avail["angr"]:
        r = qe.angr_check("x.py")
        assert r["available"] is False


# ── 新工具注册 ────────────────────────────────────────────
def test_new_tools_registered_v2():
    import server
    for t in ("repo_wiki", "agent_orchestrate", "agent_roles"):
        assert t in server._TOOLS, f"{t} 未注册"
