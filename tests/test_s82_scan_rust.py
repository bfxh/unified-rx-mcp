# -*- coding: utf-8 -*-
"""S82：scan 域轻正则三工具（std_check / ui_check / bug_locate）Rust 原生化
（rx-scan.exe）的契约测试。

薄壳转调 + exe 缺失清晰报错 + 大 error_text 走 stdin + schema 契约不变 +
旧实现内部函数确已退役。行为回归由旧 test_v2.py / test_bevy.py 原样继续
承担（薄壳下原样过检即等价证明）；正则/遍历边界语义由 rust/tests/scan_test.rs
承担（13 例）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401
import tools.scan as tscan


def _write(root, rel, content, newline=""):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline=newline)
    return p


def test_std_check_registry_contract(tmp_path):
    _write(tmp_path, "a.py", "x = 123\ns = 'foo bar'\n")
    r = registry.call("std_check", {"path": str(tmp_path)})
    assert r["ok"], r
    res = r["result"]
    assert res["files"] == 1 and res["total"] >= 2, res
    assert {"file", "line", "rule", "msg", "text"} <= set(res["findings"][0])


def test_ui_check_registry_contract(tmp_path):
    _write(tmp_path, "u.gd", "extends Button\n\nButton:\n")
    _write(tmp_path, "u.cs", "var b = new Button();\n")
    _write(tmp_path, "f.rs",
           "fn a(mut c: Commands) {\n    c.spawn((Button, OrphanM, N::default()));\n}\n")
    r = registry.call("ui_check", {"path": str(tmp_path)})
    assert r["ok"], r
    res = r["result"]
    assert res["files"] == 3, res
    assert {i["engine"] for i in res["issues"]} == {"godot", "unity", "bevy"}
    assert all({"file", "line", "rule", "msg", "engine"} <= set(i)
               for i in res["issues"])


def test_bug_locate_registry_contract(tmp_path):
    _write(tmp_path, "src/app.py", "l1\nl2\nbroken_thing()\nl4\nl5\n")
    text = ('Traceback (most recent call last):\n'
            '  File "src\\app.py", line 3, in <module>\n'
            'NameError: broken_thing')
    r = registry.call("bug_locate", {"error_text": text, "root": str(tmp_path)})
    assert r["ok"], r
    res = r["result"]
    assert res["candidates"] == 1, res
    h = res["hits"][0]
    assert h["line"] == 3 and h["how"] == "traceback 精确"
    assert {"file", "line", "how", "snippet"} <= set(h)
    assert "broken_thing()" in h["snippet"]


def test_path_not_exist_structured_error(tmp_path):
    ghost = str(tmp_path / "nope")
    for name in ("std_check", "ui_check"):
        r = registry.call(name, {"path": ghost})
        assert not r["ok"] and "路径不存在" in r["error"], (name, r)


def test_bug_locate_root_not_dir(tmp_path):
    """root 不是目录：薄壳不预判，exe 侧结构化错误（exit 0 + error 对象）。"""
    r = registry.call("bug_locate",
                      {"error_text": "x", "root": str(tmp_path / "nope")})
    assert not r["ok"] and "root 不是目录" in r["error"], r


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    """exe 缺失必须清晰报错，不静默降级（S79 既定政策）。"""
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", r"Z:\nope\rx-scan.exe")
    monkeypatch.setenv("TEMP", str(tmp_path))  # 惯例路径也指到空处
    r = registry.call("std_check", {"path": str(tmp_path)})
    assert not r["ok"] and "rx-scan.exe 不存在" in r["error"], r


def test_big_error_text_rides_stdin(tmp_path):
    """4 万字 error_text 超 argv 上限（10000 字符阈值）→ 薄壳走 stdin
    （argv 传 "-"，exe 侧等价替换），子进程绝不继承宿主协议管道。"""
    _write(tmp_path, "app.py", "a\nb\nbreak_here()\nd\ne\n")
    text = "背景" * 20000 + 'File "app.py", line 3, in f'
    r = registry.call("bug_locate", {"error_text": text, "root": str(tmp_path)})
    assert r["ok"], r
    assert r["result"]["candidates"] == 1, r
    assert r["result"]["hits"][0]["line"] == 3


def test_max_files_negative_and_noncode_quota(tmp_path):
    """负名额 ≡ 0（Python count >= max 立停语义，exe 侧 .max(0)）；
    非代码文件不占名额——md 独占根目录时名额 1 仍能扫到子目录代码文件。"""
    _write(tmp_path, "notes.md", "TODO\n")
    _write(tmp_path, "sub/a.py", "x = 111\n")
    r = registry.call("std_check", {"path": str(tmp_path), "max_files": -5})
    assert r["ok"] and r["result"]["files"] == 0 and r["result"]["total"] == 0, r
    r2 = registry.call("std_check", {"path": str(tmp_path), "max_files": 1})
    assert r2["ok"], r2
    assert r2["result"]["files"] == 1 and r2["result"]["total"] == 1, r2


def test_crlf_universal_newline_snippet(tmp_path):
    """CRLF 文件按 readlines 的 universal newlines 归一：行号与 snippet 不串 \r。"""
    _write(tmp_path, "win.py", "l1\r\nl2\r\nbroken_crlf()\r\nl4\r\n")
    r = registry.call("bug_locate",
                      {"error_text": 'File "win.py", line 3, in f',
                       "root": str(tmp_path)})
    assert r["ok"], r
    h = r["result"]["hits"][0]
    assert h["line"] == 3, r
    assert h["snippet"] == "l1\nl2\nbroken_crlf()\nl4", r


def test_project_scan_composes(tmp_path):
    """project_scan 三路组合照旧：S83 起 std/ui/bug_scan 三路全走 exe。"""
    _write(tmp_path, "a.py", "x = 123\n")
    _write(tmp_path, "u.gd", "extends Button\n\nButton:\n")
    r = registry.call("project_scan", {"path": str(tmp_path), "max_files": 10})
    assert r["ok"], r
    res = r["result"]
    assert res["std_check"]["total"] >= 1 and res["ui_check"]["total"] >= 1
    assert set(res) == {"path", "bug_scan", "std_check", "ui_check", "summary"}


def test_schema_contract():
    assert registry._TOOLS["std_check"]["group"] == "scan"
    assert registry._TOOLS["std_check"]["schema"]["required"] == ["path"]
    assert set(registry._TOOLS["std_check"]["schema"]["properties"]) == {"path", "max_files"}
    assert registry._TOOLS["ui_check"]["group"] == "scan"
    assert registry._TOOLS["ui_check"]["schema"]["required"] == ["path"]
    assert set(registry._TOOLS["ui_check"]["schema"]["properties"]) == {"path", "max_files"}
    assert registry._TOOLS["bug_locate"]["group"] == "scan"
    assert registry._TOOLS["bug_locate"]["schema"]["required"] == ["error_text"]
    assert set(registry._TOOLS["bug_locate"]["schema"]["properties"]) == {"error_text", "root"}


def test_positional_call(tmp_path):
    """薄壳保持位置参数签名 (path, max_files)，返回裸结果 dict（非包络）。"""
    _write(tmp_path, "a.py", "x = 789\n")
    r = tscan.std_check(str(tmp_path), 10)
    assert r["files"] == 1 and r["total"] == 1, r


def test_old_python_internals_retired():
    """S82/S83 前的纯 Python 实现已退役：内部函数/缓存不得复活（薄壳是唯一路径）。"""
    for name in ("_std_check_file", "_UI_PATTERNS", "_find_in_file", "_line_ctx",
                 # S83 bug_scan 全量原生化的退役面
                 "_scan_python", "_scan_rust", "_scan_generic", "_RUST_RULES",
                 "_RE_RULES", "_SCAN_CACHE", "_cached_scan", "_file_fingerprint",
                 "scan_cache_clear"):
        assert not hasattr(tscan, name), name
    # 薄壳与共享遍历助手必须保留（ide 域也用 _iter_files/_lang_of）
    for name in ("_iter_files", "_lang_of", "_rx_scan_exe", "_rx_scan_call",
                 "bug_scan", "std_check", "ui_check", "bug_locate", "project_scan"):
        assert hasattr(tscan, name), name
    import tools.bevy as tbevy
    for name in ("BEVY_UI_PATTERNS", "BEVY_CODE_PATTERNS", "find_dead_buttons"):
        assert not hasattr(tbevy, name), name
    assert hasattr(tbevy, "bevy_rules")
