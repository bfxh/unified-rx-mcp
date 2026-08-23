# -*- coding: utf-8 -*-
"""tests/test_meta.py —— meta 域测试（P1-P7 修复验证）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_blender_no_placeholder():
    """P1: blender 命令不再有未填充占位符（内置默认路径）。"""
    r = registry.call("local_run", {"domain": "blender", "name": "headless",
                                    "args": {"script": "test.py", "args": ""},
                                    "timeout": 30})
    # 应能通过校验执行（不再被"不安全字符"拒绝）
    err = r["result"].get("error", "")
    assert "不安全字符" not in err, f"不应误报不安全字符: {err}"
    assert "占位符" not in err, f"不应有未填充占位符: {err}"
    # blender 能启动（无头模式），即使脚本不存在也 exit 0
    assert r["result"].get("ok") is True or "cmd" in r["result"], f"应执行成功: {r}"


def test_blender_default_path():
    """P1: 默认 blender 路径内置（D:\\rj\\GJ\\Blender 5.2）。"""
    from tools.meta import _BLENDER
    assert os.path.exists(_BLENDER), f"默认 blender 路径应存在: {_BLENDER}"


def test_unfilled_placeholder_reported():
    """占位符未填 → 明确报错（不是误报不安全字符）。"""
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {}})  # 缺 {script}
    assert "占位符" in r["result"].get("error", ""), f"应报未填充占位符: {r}"


def test_shell_injection_blocked():
    """P6: shell 注入字符被拒。"""
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {"script": "x.py & del C:\\*"}})
    assert "不安全字符" in r["result"].get("error", ""), f"应拒绝注入: {r}"


def test_process_list():
    """P4: process list 能列进程。"""
    r = registry.call("process", {"action": "list"})
    assert r["ok"], r
    assert r["result"]["count"] > 0, "应有进程"


def test_process_list_filter():
    """P4: 按名查进程。"""
    r = registry.call("process", {"action": "list", "name": "python.exe"})
    assert r["ok"], r


def test_cheatsheet_has_blender_process():
    """P4/P5: cheatsheet 补 blender/process 域。"""
    r = registry.call("cmd_cheatsheet", {})
    assert r["ok"]
    domains = r["result"]["domains"]
    assert "blender" in domains, f"应含 blender: {domains}"
    assert "process" in domains, f"应含 process: {domains}"
