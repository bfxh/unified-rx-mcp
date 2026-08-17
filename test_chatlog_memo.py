#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_chatlog_memo.py — chatlog 采集/检索 + 备忘录留痕/相似性测试。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import chatlog_core as cl  # noqa: E402
import design_notes as dn  # noqa: E402
import pytest  # noqa: E402


def test_trace_call_roundtrip():
    root = tempfile.mkdtemp(prefix="trace_")
    try:
        r = dn.trace_call(root, "marvis", "scan", "跑了一轮 bug_scan")
        assert r["ok"] is True
        assert os.path.exists(os.path.join(root, ".unified-rx", "traces.jsonl"))
        # 留痕读取
        lst = dn.list_traces(root)
        assert lst["count"] == 1
        assert lst["traces"][0]["agent"] == "marvis"
        assert lst["traces"][0]["action"] == "scan"
        # agent 过滤
        lst2 = dn.list_traces(root, agent="other")
        assert lst2["count"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_trace_call_bad_root():
    r = dn.trace_call(r"C:\no\such\dir\xyz", "a", "b")
    assert r["ok"] is False


def test_similar_finds_note_and_trace():
    root = tempfile.mkdtemp(prefix="sim_")
    try:
        dn.add_note(root, "doubts", "lua 钩子边界可能有 bug", tag="lua")
        dn.trace_call(root, "qoder", "fix", "修复了 lua 钩子边界问题")
        r = dn.similar_notes(root, "lua")
        assert r["ok"] is True
        assert r["hit_count"] >= 2, r
        sources = {h["source"] for h in r["hits"]}
        assert "note" in sources and "trace" in sources
        # 不相似 query 无命中
        r2 = dn.similar_notes(root, "zzz-nonexistent-xyz")
        assert r2["hit_count"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_chatlog_search_index():
    # 索引文件存在（本机已采集）
    assert os.path.exists(cl.CHATLOG)
    r = cl.search("", limit=5)
    assert r["ok"] is True
    assert r["count"] > 0
    # agent 过滤合法（不炸）
    r2 = cl.search("", agent="marvis", limit=5)
    assert r2["ok"] is True


def test_chatlog_append_dedup():
    # 同一记录重复追加不重复
    recs = [{"agent": "test", "ts": 1, "title": "t", "text": "hello",
             "hash": "abc123"}]
    n1 = cl._append(recs)
    n2 = cl._append(recs)
    assert n1 == 1
    assert n2 == 0  # 已存在去重
    # 清理测试记录
    lines = [ln for ln in open(cl.CHATLOG, encoding="utf-8")
             if '"agent": "test"' not in ln]
    open(cl.CHATLOG, "w", encoding="utf-8").writelines(lines)
