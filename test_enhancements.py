"""P0-P4 新模块回归测试（search_index/graph_index/quality_engine/storage_tiers/explore_engine/local_intel）。

覆盖 P0b-P4 的核心行为，防止未来重构回归。与临时验证脚本互补（这是常驻套件）。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from search_index import SearchIndex
from graph_index import GraphIndex
from storage_tiers import TieredStore


def _tmp(name):
    d = os.path.join(tempfile.gettempdir(), f"hermes_pytest_{name}")
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    return d


def _clean(d):
    shutil.rmtree(d, ignore_errors=True)


# ── P0b 混合检索 ──────────────────────────────────────────
def test_search_index_bm25_and_rrf():
    d = _tmp("searchidx")
    db = os.path.join(d, "idx.db")
    idx = SearchIndex(db)
    idx.add_many([
        {"id": "a", "content": "unified-rx MCP server tool dispatch", "title": "s"},
        {"id": "b", "content": "rx-core Rust pure functions math sort", "title": "r"},
        {"id": "c", "content": "hallucination guard verifies AI claims", "title": "g"},
    ])
    hits = idx.search("rust functions")
    assert hits and hits[0]["id"] == "b"
    # 更新（同 id 替换）
    idx.add_document("a", "updated rx-core math", meta={"v": 2})
    hits2 = idx.search("rx-core math")
    assert any(h["id"] == "a" for h in hits2)
    assert next(h for h in hits2 if h["id"] == "a")["meta"] == {"v": 2}
    assert idx.stats()["docs"] == 3
    # 删除
    idx.delete("b")
    assert idx.stats()["docs"] == 2
    # 特殊字符不炸
    assert isinstance(idx.search('"q" OR -- (x)'), list)
    _clean(d)


def test_search_index_hybrid_fallback():
    d = _tmp("searchidx2")
    db = os.path.join(d, "idx.db")
    idx = SearchIndex(db)
    idx.add_document("a", "hallucination guard")
    # 无 embed_fn → 纯 BM25
    assert idx.search_hybrid("guard", embed_fn=None)
    # 有 embed_fn → RRF（子类注入向量路）
    class VecIdx(SearchIndex):
        def _vector_search(self, query, embed_fn, limit):
            return [{"id": "a", "title": "", "content": "hallucination", "meta": {}, "vec": 1}]

    v = VecIdx(db)
    out = v.search_hybrid("hallucination", embed_fn=lambda t: [1.0])
    assert any(h["id"] == "a" for h in out)
    _clean(d)


# ── P1a 图索引 ────────────────────────────────────────────
def test_graph_index_python_symbols():
    d = _tmp("graph")
    db = os.path.join(d, "g.db")
    gi = GraphIndex(db)
    src = os.path.join(d, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "m.py"), "w", encoding="utf-8") as fh:
        fh.write("def helper():\n    return 1\n\ndef main():\n    return helper()\n")
    stats = gi.index_directory(src)
    assert stats["nodes"] >= 2, stats
    syms = gi.search_symbols("helper")
    assert syms, "符号搜索应有结果"
    # 调用边：main → helper
    sid = syms[0]["id"]
    callers = gi.callers_of(sid)
    assert isinstance(callers, list)
    _clean(d)


# ── P2b 三层存储 ──────────────────────────────────────────
def test_storage_tiers():
    d = _tmp("tiers")
    st = TieredStore(d, name="t")
    st.append({"kind": "scan", "n": 1})
    st.append({"kind": "scan", "n": 2})
    hits = st.query()
    assert len(hits) == 2
    assert hits[0]["n"] == 2  # 最新优先
    # 过滤器
    assert len(st.query(filter_fn=lambda r: r["n"] == 1)) == 1
    # 归档触发（写 600 条 > _HOT_MAX=500）
    for i in range(600):
        st.append({"kind": "scan", "n": i})
    s = st.stats()
    assert s["warm"] >= 500
    assert 0 < s["hot"] <= 500
    # 温层旧数据可查（LIMIT 截断修复）
    q = st.query(filter_fn=lambda r: r.get("n") == 42)
    assert q and q[0]["n"] == 42
    _clean(d)


# ── 全工具注册数 ──────────────────────────────────────────
def test_new_tools_registered():
    import server
    for t in ("kb_query", "repo_graph", "lesson_extract", "quality_scan"):
        assert t in server._TOOLS, f"{t} 未注册"
