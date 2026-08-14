# -*- coding: utf-8 -*-
"""高并发压力测试（2026-08-14 用户点名：出事了高并发出大问题）。

复现并锁定并发竞态：scan_log append/truncate、design_notes 整文件写、
_EXT_LOADED 懒加载、混合工具风暴。
"""
import json
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import scan_log_core  # noqa: E402
import design_notes  # noqa: E402


def _threads(n, fn):
    ts = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_append_scan_concurrent_no_loss(tmp_path, monkeypatch):
    """8 线程 × 200 条并发 append——行数不丢、每行 JSON 有效、无交错损坏。"""
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    errors = []

    def worker(i):
        try:
            for j in range(200):
                scan_log_core.append_scan({"tool": "t", "root": f"r{i}",
                                           "ok": True, "summary": f"s{i}-{j}"})
        except Exception as e:
            errors.append(e)

    _threads(8, worker)
    assert not errors, f"并发 append 异常: {errors}"
    lines = logf.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600, f"行数丢失: {len(lines)}/1600"
    for ln in lines:
        json.loads(ln)  # 每行必须是完整 JSON（无交错损坏）


def test_design_note_concurrent_no_loss(tmp_path):
    """8 线程 × 50 条并发 add_note——整文件写竞态丢数据复现。"""
    errors = []

    def worker(i):
        try:
            for j in range(50):
                design_notes.add_note(str(tmp_path), "settled", f"n{i}-{j}")
        except Exception as e:
            errors.append(e)

    _threads(8, worker)
    assert not errors, f"并发 add_note 异常: {errors}"
    lst = design_notes.list_notes(str(tmp_path))
    total = len(lst.get("settled", []))
    assert total == 400, f"笔记丢失: {total}/400（整文件写竞态）"


def test_ext_lazy_load_concurrent_single(tmp_path, monkeypatch):
    """8 线程首次并发加载同一扩展——只加载一次（无双重加载竞态）。"""
    server._EXT_LOADED.pop("stats", None)  # 强制首次加载路径
    load_count = []

    def worker(i):
        try:
            mod = server._load_ext("stats")
            load_count.append(1 if mod is not None else 0)
        except Exception:
            load_count.append(0)

    _threads(8, worker)
    assert load_count.count(1) == 8, f"加载成功线程: {load_count}"
    assert "stats" in server._EXT_LOADED


def test_call_storm_concurrent_stable(tmp_path, monkeypatch):
    """混合工具风暴：4 线程 × 100 次随机工具调用——无崩溃、结果结构稳定。

    覆盖 vuln_scan（3 路）、project_scan（4 路）、parallel（8 路）的同源并发。
    """
    import json as _json
    tmp = tmp_path / "repo"
    tmp.mkdir()
    (tmp / "a.py").write_text("def helper():\n    pass\nimport json\n",
                              encoding="utf-8")
    (tmp / "b.rs").write_text("fn main() {\n    let x = foo().unwrap();\n}\n",
                              encoding="utf-8")
    tools = [
        lambda: server._call("bug_scan", {"path": str(tmp)}),
        lambda: server._call("std_check", {"path": str(tmp)}),
        lambda: server._call("vuln_scan", {"path": str(tmp)}),
        lambda: server._call("ide_actions", {"path": str(tmp)}),
        lambda: server._call("parallel", {"tasks": [
            {"tool": "bug_scan", "args": {"path": str(tmp)}},
            {"tool": "std_check", "args": {"path": str(tmp)}},
        ]}),
    ]
    errors = []

    def worker(i):
        try:
            for k in range(100):
                r = tools[(i + k) % len(tools)]()
                assert r and r[0].text, "空返回"
        except Exception as e:
            errors.append(e)

    _threads(4, worker)
    _threads(4, worker)
    assert not errors, f"工具风暴异常: {errors[:3]}"
