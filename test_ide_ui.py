#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_ui（桌面 IDE 壳）非 GUI 部分测试：语言探测/JSON 格式化/忽略清单。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ide_ui  # noqa: E402


def test_lang_detect():
    assert ide_ui._lang_of("a.py") == "python"
    assert ide_ui._lang_of("b.rs") == "rust"
    assert ide_ui._lang_of("c.json") == "json"
    assert ide_ui._lang_of("d.txt") == ""


def test_fmt_json():
    assert ide_ui._fmt_json({"a": 1}) == '{\n "a": 1\n}'
    assert "Error" not in ide_ui._fmt_json(object())  # 异常兜底


def test_skip_dirs():
    for d in (".git", "target", "__pycache__", "node_modules", "vendor"):
        assert d in ide_ui._SKIP_DIRS


def test_keyword_patterns_compile():
    import re
    for lang in ("python", "rust", "json"):
        assert re.compile(ide_ui._KW[lang])
    assert re.compile(ide_ui._STR_RE, re.S)


def test_import_dashboard_reuse():
    # ide_ui 复用 dashboard 的纯读取函数（零重复）
    assert callable(ide_ui._read_stats)
    assert callable(ide_ui._scanlog)
    assert callable(ide_ui._telemetry)
