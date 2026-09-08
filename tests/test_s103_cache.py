# -*- coding: utf-8 -*-
"""S103 内容寻址增量缓存契约（tools/cache.py + registry.call 接线）。

核心承诺：缓存只影响延迟、不影响语义——命中结果与冷跑**逐字节一致**；
文件一变指纹就变（失效）；写/执行类工具永不入缓存；越界调用不入缓存。
"""
import json
import os

import pytest

import registry
import tools  # noqa: F401
from tools import cache as cache_mod


@pytest.fixture(autouse=True)
def _clean_cache():
    cache_mod.clear()
    cache_mod.reset_stats()
    yield
    cache_mod.clear()
    cache_mod.reset_stats()


def _mkproj(tmp_path, body="def f():\n    return 1\n"):
    f = tmp_path / "m.py"
    f.write_text(body, encoding="utf-8")
    return f


def _call(name, args):
    r = registry.call(name, args)
    assert r.get("ok"), r
    return r


def test_hit_is_byte_identical_and_counted(tmp_path):
    _mkproj(tmp_path)
    a1 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    a2 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    assert json.dumps(a1, ensure_ascii=False) == json.dumps(a2, ensure_ascii=False)
    st = cache_mod.stats()
    assert st["hits"] == 1 and st["puts"] == 1, st


def test_edit_invalidates(tmp_path):
    f = _mkproj(tmp_path)
    _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    hits0 = cache_mod.stats()["hits"]
    f.write_text("def f():\n    try:\n        pass\n    except:\n        pass\n",
                 encoding="utf-8")
    r = _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    st = cache_mod.stats()
    assert st["hits"] == hits0, "文件变更后必须未命中"
    assert st["puts"] >= 2, st
    # 且新结果确实反映了新内容（bare_except 命中）
    assert any(i.get("rule") == "bare_except"
               for i in (r["result"].get("issues") or [])), r["result"]


def test_args_partition(tmp_path):
    _mkproj(tmp_path)
    _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    hits0 = cache_mod.stats()["hits"]
    _call("bug_scan", {"path": str(tmp_path), "max_files": 20})
    assert cache_mod.stats()["hits"] == hits0, "参数不同必须另起键"


def test_no_cache_transport_param(tmp_path):
    _mkproj(tmp_path)
    r1 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    st = cache_mod.stats()
    r2 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10,
                            "__no_cache": True})
    st2 = cache_mod.stats()
    assert json.dumps(r1, ensure_ascii=False) == json.dumps(r2, ensure_ascii=False)
    assert st2["hits"] == st["hits"] and st2["puts"] == st["puts"], (st, st2)


def test_env_disable(tmp_path, monkeypatch):
    _mkproj(tmp_path)
    monkeypatch.setenv("UNIFIED_RX_NO_CACHE", "1")
    _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    st = cache_mod.stats()
    assert st["puts"] == 0 and st["hits"] == 0, st


def test_out_of_sandbox_not_cached():
    r = registry.call("bug_scan", {"path": r"C:\Windows", "max_files": 5})
    assert r.get("ok") is False
    st = cache_mod.stats()
    assert st["puts"] == 0, st


def test_code_search_root_arg_cached(tmp_path):
    _mkproj(tmp_path, "def alpha_marker():\n    pass\n")
    a1 = _call("code_search", {"query": "alpha_marker", "root": str(tmp_path), "k": 5})
    a2 = _call("code_search", {"query": "alpha_marker", "root": str(tmp_path), "k": 5})
    assert json.dumps(a1, ensure_ascii=False) == json.dumps(a2, ensure_ascii=False)
    assert cache_mod.stats()["hits"] == 1


def test_cursor_pagination_not_confused_by_cache(tmp_path):
    # S103 全量测试实锤的回归：cursor 是传输层参数、算键前已被剥除，
    # 若不入键则第 2 页会命中第 1 页缓存。
    body = "".join(f"try:\n    x{i} = 1\nexcept:\n    pass\n" for i in range(210))
    (tmp_path / "many.py").write_text(body, encoding="utf-8")
    p1 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10})
    assert p1["result"]["truncated"] is True
    assert p1["result"]["next_cursor"] == 200
    p2 = _call("bug_scan", {"path": str(tmp_path), "max_files": 10, "cursor": 200})
    assert len(p1["result"]["issues"]) == 200
    assert 0 < len(p2["result"]["issues"]) < 200
    assert p2["result"]["issues"][0]["line"] != p1["result"]["issues"][0]["line"]


def test_write_tools_not_cacheable():
    for name in ("fs_write", "local_run", "ide_edit_multi", "app_clean"):
        assert not cache_mod.cacheable(name), name


def test_fingerprint_changes_with_content(tmp_path):
    f = _mkproj(tmp_path)
    fp1 = cache_mod.fingerprint(str(tmp_path))
    f.write_text("def f():\n    return 2\n", encoding="utf-8")
    fp2 = cache_mod.fingerprint(str(tmp_path))
    assert fp1 and fp2 and fp1 != fp2


def test_fingerprint_file_root(tmp_path):
    f = _mkproj(tmp_path)
    fp1 = cache_mod.fingerprint(str(f))
    f.write_text("def f():\n    return 3\n", encoding="utf-8")
    assert fp1 and cache_mod.fingerprint(str(f)) != fp1
