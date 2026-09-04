# -*- coding: utf-8 -*-
"""S73：深度扫描（Mimosa scan-2026-09-04T15-42-49, seal 32bfc234）实锤三处修复的回归测试。

1. code_coverage 跑任意脚本却无授权门 + script/source_dir 只 abspath 不过沙盒
   （违反 S62"跑程序=任意代码执行必须授权"）→ requires_auth + _fs_resolve
2. lesson 显式 lessons_dir 可任意路径写 JSONL → 过沙盒（默认库路径固定可信免检）
3. app_clone 整目录读取（fs_read 够不着的隐私面）无授权 → requires_auth
4. dep_graph / module_stability 读路径同样钳进沙盒（核实扫描误报时发现的同类缺口）
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
import tools  # noqa: E402,F401  注册全部

越界 = "越界"


def _declared(name):
    """S72b 契约：requires_auth 工具在 list_tools 里必须声明 __authorized。"""
    for t in registry.list_tools():
        if t["name"] == name:
            props = t["inputSchema"].get("properties") or {}
            req = t["inputSchema"].get("required") or []
            return "__authorized" in props and "__authorized" in req
    return False


# ---------- code_coverage：授权门 ----------

def test_code_coverage_requires_auth(tmp_path):
    r = registry.call("code_coverage", {"script": str(tmp_path / "x.py"),
                                        "source_dir": str(tmp_path)})
    assert r["ok"] is False and "授权" in r["error"], r


def test_code_coverage_declares_authorized_in_schema():
    assert _declared("code_coverage")


# ---------- code_coverage：沙盒钳制 ----------

def test_code_coverage_paths_outside_sandbox_denied():
    r = registry.call("code_coverage", {"script": r"C:/Windows/notepad.exe",
                                        "source_dir": r"C:/Windows",
                                        "__authorized": True})
    assert r["ok"] is False and 越界 in r["error"], r


def test_code_coverage_happy_path_inside_sandbox(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mod.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    script = tmp_path / "run.py"
    script.write_text("import mod\nprint(mod.add(1, 2))\n", encoding="utf-8")
    r = registry.call("code_coverage", {"script": str(script),
                                        "source_dir": str(proj),
                                        "__authorized": True})
    assert r["ok"] is True, r
    res = r["result"]
    assert res["covered_lines"] > 0 and res["total_lines"] >= 2
    assert any(f["file"] == "mod.py" for f in res["per_file"])


# ---------- lesson：显式 lessons_dir 沙盒钳制 ----------

def test_lesson_explicit_dir_outside_sandbox_denied():
    r = registry.call("lesson", {"action": "add", "text": "S73 测试",
                                 "lessons_dir": r"C:/Windows/urx_s73_test.jsonl"})
    assert r["ok"] is False and 越界 in r["error"], r


def test_lesson_explicit_dir_inside_sandbox_ok(tmp_path):
    lp = tmp_path / "lessons.jsonl"
    r = registry.call("lesson", {"action": "add", "text": "S73 沙盒内可写",
                                 "lessons_dir": str(lp)})
    assert r["ok"] is True, r
    assert lp.exists()


def test_lesson_default_dir_still_works(tmp_path, monkeypatch):
    import tools.learn as learn
    fake = tmp_path / "default.jsonl"
    monkeypatch.setattr(learn, "_DEFAULT_LESSONS", str(fake))
    r = registry.call("lesson", {"action": "add", "text": "S73 默认库免检可用"})
    assert r["ok"] is True and fake.exists(), r


# ---------- app_clone：授权门 ----------

def test_app_clone_requires_auth(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    r = registry.call("app_clone", {"source_dir": str(d)})
    assert r["ok"] is False and "授权" in r["error"], r


def test_app_clone_declares_authorized_in_schema():
    assert _declared("app_clone")


# ---------- dep_graph / module_stability：读路径钳制 ----------

def test_dep_graph_outside_sandbox_denied():
    r = registry.call("dep_graph", {"path": r"C:/Windows"})
    assert r["ok"] is False and 越界 in r["error"], r


def test_module_stability_outside_sandbox_denied():
    r = registry.call("module_stability", {"path": r"C:/Windows"})
    assert r["ok"] is False and 越界 in r["error"], r
