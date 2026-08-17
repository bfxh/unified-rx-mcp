#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_local_tools.py — 本地工具注册表/调用桥测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import local_tools as lt  # noqa: E402


def test_scan_discovers_tools():
    r = lt.scan()
    assert r["ok"] is True
    assert r["count"] > 100  # D:\rj 工具丰富
    names = {t["name"] for t in r["tools"]}
    # 7zip 在工具根（7z2602-x64 或 7za）
    assert names & {"7z2602-x64", "7za"}, names


def test_discover_filter():
    r = lt.discover(query="node")
    assert r["ok"] is True
    assert all("node" in t["name"] or "node" in t["dir"].lower() for t in r["tools"])


def test_run_known_tool():
    r = lt.run("node", ["--version"], timeout=10)
    assert r["ok"] is True
    assert r["exit_code"] == 0
    assert "v" in r["stdout"]


def test_run_unknown_tool():
    r = lt.run("definitely-not-a-tool-xyz", [], timeout=5)
    assert r["ok"] is False
    assert "未注册" in r["error"]


def test_run_dangerous_args_rejected():
    for bad in (["rm -rf /"], ["format C:"], ["del /f /s /q C:\\*"]):
        r = lt.run("node", bad, timeout=5)
        assert r["ok"] is False
        assert "拒绝" in r["error"], bad


def test_run_bad_args_type():
    r = lt.run("node", "not-a-list", timeout=5)
    assert r["ok"] is False


def test_run_bad_timeout():
    r = lt.run("node", [], timeout=9999)
    assert r["ok"] is False
