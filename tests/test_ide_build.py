# -*- coding: utf-8 -*-
"""S32 ide_build / ide_debug：编译与调试捕获回归。"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools.ide import _parse_py_traceback, _parse_rust_panic  # noqa: E402

CARGO = None
try:
    CARGO = __import__("shutil").which("cargo")
except Exception:
    CARGO = None


def call_tool(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    """registry 对工具 error/ok:false 特判（S7/S10 契约）——统一取值并把 ok 合回。"""
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


# ---------- 纯解析器 ----------

def test_parse_py_traceback():
    tb = ('Traceback (most recent call last):\n'
          '  File "app/main.py", line 10, in run\n'
          '    go()\n'
          '  File "app/util.py", line 22, in go\n'
          '    1/0\n'
          'ZeroDivisionError: division by zero\n')
    frames, last = _parse_py_traceback(tb)
    assert frames[0]["file"].endswith("main.py") and frames[0]["line"] == 10
    assert frames[1]["fn"] == "go"
    assert "ZeroDivisionError" in last and "division by zero" in last


def test_parse_rust_panic_with_backtrace():
    txt = ('thread \'main\' panicked at src/engine/drive.rs:42:13:\n'
           'attempt to subtract with overflow\n'
           'stack backtrace:\n'
           '   0: core::panicking::panic\n'
           '   1: vf3::engine::drive::apply\n'
           '             at ./src/engine/drive.rs:42:13\n'
           '   2: vf3::main\n'
           '             at ./src/main.rs:8:5\n')
    pans = _parse_rust_panic(txt)
    assert len(pans) == 1
    p = pans[0]
    assert p["file"].endswith("drive.rs") and p["line"] == 42
    assert "subtract with overflow" in p["msg"]
    assert p["backtrace"][0]["line"] == 42


def test_parse_rust_panic_old_format():
    pans = _parse_rust_panic("thread 'main' panicked at 'boom here', src/x.rs:7:5")
    assert pans[0]["msg"] == "boom here" and pans[0]["line"] == 7


# ---------- ide_build：python 语法检查（全平台可跑） ----------

def test_ide_build_python_catches_syntax_error(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["tool"] == "compileall" and r["ok"] is False
    assert r["errors"] and any(e["line"] for e in r["errors"])


def test_ide_build_python_ok(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    r = call_tool("ide_build", {"path": str(tmp_path)})
    assert r["ok"] is True and r["errors"] == []


def test_ide_build_sandbox_rejects_outside(tmp_path):
    r = call_tool("ide_build", {"path": "C:/Windows"})
    assert "error" in r


def test_ide_debug_cmd_must_be_list(tmp_path):
    r = call_tool("ide_debug",
                  {"path": str(tmp_path), "cmd": "evil; rm"})
    assert "error" in r and "array" in r["error"]


# ---------- ide_debug：python traceback 端到端 ----------

def test_ide_debug_python_crash(tmp_path):
    (tmp_path / "crash.py").write_text(
        "def boom():\n    return 1 / 0\n\nboom()\n", encoding="utf-8")
    py = sys.executable
    r = call_tool("ide_debug",
                  {"path": str(tmp_path), "cmd": [py, "crash.py"]})
    assert r["ok"] is False and r["python"]
    assert any(f["fn"] == "boom" for f in r["python"]["frames"])
    assert "ZeroDivisionError" in r["python"]["last_error"]


# ---------- ide_build：cargo（有 cargo 才跑；新 crate 秒级） ----------

@pytest.mark.skipif(CARGO is None, reason="no cargo toolchain")
def test_ide_build_cargo_catches_error(tmp_path):
    proj = tmp_path / "hello"
    subprocess.run([CARGO, "init", "--name", "hello", str(proj)], check=True,
                   capture_output=True)
    (proj / "src" / "main.rs").write_text(
        "fn main() {\n    let x: i32 = \"not a number\";\n}\n", encoding="utf-8")
    r = call_tool("ide_build", {"path": str(proj)})
    assert r["tool"] == "cargo" and r["ok"] is False
    assert any(d["level"] == "error" for d in r["errors"])


@pytest.mark.skipif(CARGO is None, reason="no cargo toolchain")
def test_ide_debug_rust_panic_end_to_end(tmp_path):
    proj = tmp_path / "panicme"
    subprocess.run([CARGO, "init", "--name", "panicme", str(proj)], check=True,
                   capture_output=True)
    (proj / "src" / "main.rs").write_text(
        "fn main() {\n    let v: Vec<i32> = vec![1];\n"
        "    println!(\"{}\", v[9]);\n}\n", encoding="utf-8")
    subprocess.run([CARGO, "build", "--quiet"], cwd=str(proj), check=True,
                   capture_output=True)
    exe = os.path.join(str(proj), "target", "debug",
                       "panicme.exe" if os.name == "nt" else "panicme")
    r = call_tool("ide_debug",
                  {"path": str(proj), "cmd": [exe], "timeout": 120})
    assert r["ok"] is False and r["rust_panics"]
    p = r["rust_panics"][0]
    assert "main.rs" in p["file"] and p["line"] == 3
