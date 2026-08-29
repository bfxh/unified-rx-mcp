# -*- coding: utf-8 -*-
"""S33 多语言 ide_build/ide_debug + swe_repair 结构化帧回归。"""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools.ide import (  # noqa: E402
    _parse_java_trace, _parse_go_panic, _parse_gcc)

import swe_repair  # noqa: E402

JAVAC = shutil.which("javac")
JAVA = shutil.which("java")
GO = shutil.which("go")
GCC = shutil.which("gcc")


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    """registry 对工具 error/ok:false 特判（S7/S10 契约）——统一取值并把 ok 合回。"""
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


# ---------- 纯解析器 ----------

def test_parse_java_trace():
    tb = ("java.lang.NullPointerException: x is null\n"
          "    at com.foo.Bar.baz(Bar.java:42)\n"
          "    at com.foo.App.main(App.java:9)\n")
    frames, last = _parse_java_trace(tb)
    assert frames[0]["cls"] == "com.foo.Bar.baz"
    assert frames[0]["line"] == 42
    assert "NullPointerException" in last


def test_parse_go_panic():
    txt = ("panic: runtime error: index out of range [9] with length 1\n\n"
           "goroutine 1 [running]:\n"
           "main.main()\n"
           "        /src/app/main.go:7 +0x3c\n")
    pans = _parse_go_panic(txt)
    assert "index out of range" in pans[0]["msg"]
    assert pans[0]["backtrace"][0]["file"].endswith("main.go")
    assert pans[0]["backtrace"][0]["line"] == 7


def test_parse_gcc_format():
    diags = _parse_gcc("main.c:5:9: error: 'x' undeclared\n"
                       "util.c:2:1: warning: unused variable 'y'", ".")
    assert len(diags) == 2
    assert diags[0]["level"] == "error" and diags[0]["line"] == 5
    assert diags[1]["level"] == "warning"


def test_structured_frames_python():
    tb = ('Traceback (most recent call last):\n'
          '  File "a.py", line 3, in boom\n'
          '    1/0\n'
          'ZeroDivisionError: division by zero\n')
    txt = swe_repair._structured_frames(tb)
    assert "[STRUCTURED FRAMES · python]" in txt
    assert "a.py:3" in txt and "ZeroDivisionError" in txt


def test_structured_frames_go_and_empty():
    txt = swe_repair._structured_frames(
        "panic: bad\n\ngoroutine 1 [running]:\nmain.main()\n\t/src/main.go:7")
    assert "[STRUCTURED FRAMES · go]" in txt
    assert swe_repair._structured_frames("no crash here plain output") == ""


# ---------- javac 集成 ----------

@pytest.mark.skipif(JAVAC is None, reason="no javac")
def test_javac_build_catches_error(tmp_path):
    (tmp_path / "Bad.java").write_text(
        "public class Bad { public void f() { int x = \"s\"; } }\n",
        encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["tool"] == "javac" and r["ok"] is False
    assert any(d["level"] == "error" for d in r["errors"])


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no JDK")
def test_ide_debug_java_trace_end_to_end(tmp_path):
    src = ("public class Boom {\n"
           "    public static void main(String[] a) {\n"
           "        int[] v = new int[1];\n"
           "        System.out.println(v[9]);\n"
           "    }\n"
           "}\n")
    (tmp_path / "Boom.java").write_text(src, encoding="utf-8")
    rc = subprocess.run(
        [JAVAC, "-d", str(tmp_path), str(tmp_path / "Boom.java")],
        capture_output=True)
    assert rc.returncode == 0
    r = call_tool("ide_debug",
                  {"path": str(tmp_path),
                   "cmd": [JAVA, "-cp", str(tmp_path), "Boom"],
                   "timeout": 120})
    lf = r["lang_frames"]
    assert lf["kind"] == "java"
    assert any(f["cls"] == "Boom.main" for f in lf["frames"])
    assert "ArrayIndexOutOfBoundsException" in lf["last_error"]


# ---------- go / gcc 集成 ----------

@pytest.mark.skipif(GO is None, reason="no go toolchain")
def test_go_build_error_parsed(tmp_path):
    (tmp_path / "go.mod").write_text(
        "module app\n\ngo 1.21\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        "package main\n\nfunc main() {\n    var x int = \"s\"\n}\n",
        encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["tool"] == "go" and r["ok"] is False
    assert r["errors"] and r["errors"][0]["line"] == 4


@pytest.mark.skipif(GCC is None, reason="no gcc")
def test_gcc_build_catches_error(tmp_path):
    (tmp_path / "bad.c").write_text(
        'int main(void) { return "s"; }\n', encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["ok"] is False
    assert any(d["level"] == "error" for d in r["errors"])


@pytest.mark.skipif(not shutil.which("g++"), reason="no g++")
def test_gxx_build_catches_error(tmp_path):
    (tmp_path / "bad.cpp").write_text(
        'int main() { return "s"; }\n', encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["ok"] is False
    assert any(d["level"] == "error" for d in r["errors"])
