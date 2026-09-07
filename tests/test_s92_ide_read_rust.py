# -*- coding: utf-8 -*-
"""S92 ide_read 双件 Rust 化契约测试：薄壳（tools/ide_read.py）→ rx-ide.exe。

S92 起旧 Python 实现的职责移入 Rust（语义对齐 tools/scan.py::_symbol_spans，
tests at rust/tests/ide_test.rs 打同一语义）；本文件守住 Python 侧注册面契约：
- 沙盒拒绝 = ValueError 包络（registry ok:false），工具级错误 = result.error 字段；
- 符号语义怪癖原样保留（一行 fn 含 struct 翻 type / js class 落 fn /
  impl fmt::Display 捕获名为 fmt / params 计数 outline 只认 fn、read_symbol 只认括号）；
- CRLF / 尾幻影行 / unicode 标识符字节级对齐；
- exe 缺失走清晰报错，不静默降级。
"""
import os

import pytest

import registry
import tools  # noqa: F401


@pytest.fixture()
def open_sandbox(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")


@pytest.fixture()
def codebase(tmp_path):
    (tmp_path / "s.py").write_text(
        "def top(a, b):\n    return a\n\n\n"
        "class Outer:\n    def handle(self, x):\n        return x\n\n"
        "    def handle(self, y, z):\n        return y + z\n",
        encoding="utf-8")
    (tmp_path / "q.rs").write_text(
        "struct Point(i32, i32);\n\n"
        "impl fmt::Display for Point {\n    fn fmt(&self) {}\n}\n\n"
        "fn q() { struct S; }\n",
        encoding="utf-8")
    (tmp_path / "w.js").write_text(
        "class Widget {\n  method(x) {\n    return x;\n  }\n}\n\n"
        "  function tail(x) {\n    return x;\n  }\n",
        encoding="utf-8")
    (tmp_path / "u.py").write_text(
        "def 你好(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "crlf.py").write_bytes(b"def one(x):\r\n    return x\r\n")
    (tmp_path / "t.ts").write_text("const x: number = 1;\n", encoding="utf-8")
    (tmp_path / "n.txt").write_text("not code", encoding="utf-8")
    return tmp_path


# ---------- ide_outline ----------

def test_outline_python_symbols(codebase, open_sandbox):
    r = registry.call("ide_outline", {"file": str(codebase / "s.py")})
    assert r["ok"], r
    res = r["result"]
    assert res["lang"] == "python" and res["total"] == 4
    syms = {s["name"]: s for s in res["symbols"]}
    assert syms["top"]["kind"] == "fn" and syms["top"]["params"] == 2
    assert syms["top"]["line"] == 1 and syms["top"]["end_line"] == 2
    assert syms["Outer"]["kind"] == "type" and syms["Outer"]["params"] == 0
    # 同名 handle 按出现序列表（第 6 / 9 行各一处）
    lines = [s["line"] for s in res["symbols"] if s["name"] == "handle"]
    assert lines == [6, 9]


def test_outline_rust_quirks(codebase, open_sandbox):
    r = registry.call("ide_outline", {"file": str(codebase / "q.rs")})
    assert r["ok"], r
    syms = {s["name"]: s for s in r["result"]["symbols"]}
    # 一行 fn q() { struct S; } —— 名字 q 但 kind 翻 type（S92 怪癖原样保留）
    assert syms["q"]["kind"] == "type" and syms["q"]["params"] == 0
    # 元组 struct header 有括号但 kind=type → outline params 0
    assert syms["Point"]["kind"] == "type" and syms["Point"]["params"] == 0
    # 符号清单恰为 Point / fmt / q：impl 头捕获名为 fmt（正则 \w+ 停在冒号），
    # 缩进在 impl 里的 fn fmt(&self) 不重复捕获（rust 只认列 0）
    names = [s["name"] for s in r["result"]["symbols"]]
    assert names == ["Point", "fmt", "q"], names
    assert r["result"]["symbols"][1]["line"] == 3  # 来自 impl 头而非方法行


def test_outline_js_and_typescript(codebase, open_sandbox):
    r = registry.call("ide_outline", {"file": str(codebase / "w.js")})
    assert r["ok"], r
    syms = {s["name"]: s for s in r["result"]["symbols"]}
    assert syms["Widget"]["kind"] == "fn"  # js class 不含 struct/... 关键字 → fn
    assert "method" not in syms            # 简写方法没有 function 关键字
    assert "tail" in syms                  # 缩进 function 捕获（js 允许缩进）
    r2 = registry.call("ide_outline", {"file": str(codebase / "t.ts")})
    assert r2["ok"] and r2["result"]["lang"] == "typescript"
    assert r2["result"]["total"] == 0 and r2["result"]["symbols"] == []


def test_outline_unicode_identifier(codebase, open_sandbox):
    r = registry.call("ide_outline", {"file": str(codebase / "u.py")})
    assert r["ok"], r
    assert r["result"]["symbols"][0]["name"] == "你好"


# ---------- ide_read_symbol ----------

def test_read_symbol_basic_and_occurrence(codebase, open_sandbox):
    r = registry.call("ide_read_symbol",
                      {"file": str(codebase / "s.py"), "name": "handle",
                       "occurrence": 2})
    assert r["ok"], r
    res = r["result"]
    assert res["start"] == 9 and res["lines"] == 2 and res["params"] == 3
    assert res["content"] == "    def handle(self, y, z):\n        return y + z"
    r1 = registry.call("ide_read_symbol",
                       {"file": str(codebase / "s.py"), "name": "handle"})
    assert r1["ok"] and r1["result"]["start"] == 6  # occurrence 缺省 = 1


def test_read_symbol_params_ignores_kind(codebase, open_sandbox):
    # read_symbol 的 params 只看括号不看 kind：元组 struct → 2（outline 里是 0）
    r = registry.call("ide_read_symbol",
                      {"file": str(codebase / "q.rs"), "name": "Point"})
    assert r["ok"], r
    assert r["result"]["kind"] == "type" and r["result"]["params"] == 2
    # 一行 fn q 的 params=1（有括号），尽管 kind=type
    r2 = registry.call("ide_read_symbol",
                       {"file": str(codebase / "q.rs"), "name": "q"})
    assert r2["ok"] and r2["result"]["params"] == 1


def test_read_symbol_crlf_and_unicode(codebase, open_sandbox):
    r = registry.call("ide_read_symbol",
                      {"file": str(codebase / "crlf.py"), "name": "one"})
    assert r["ok"], r
    assert r["result"]["content"] == "def one(x):\n    return x"  # 无 \r
    r2 = registry.call("ide_read_symbol",
                       {"file": str(codebase / "u.py"), "name": "你好"})
    assert r2["ok"] and r2["result"]["name"] == "你好"


def test_read_symbol_trailing_phantom_line(codebase, open_sandbox):
    # 末位符号（缩进 function，无 brace 回扫）的跨度含尾幻影行 → content 以 \n 收尾
    r = registry.call("ide_read_symbol",
                      {"file": str(codebase / "w.js"), "name": "tail"})
    assert r["ok"], r
    assert r["result"]["content"].endswith("\n")


# ---------- 工具级错误（result.error → registry ok:false） ----------

def test_missing_file_and_not_code(codebase, open_sandbox):
    r = registry.call("ide_outline", {"file": str(codebase / "no.py")})
    assert not r["ok"] and "文件不存在" in r["error"]
    r2 = registry.call("ide_outline", {"file": str(codebase / "n.txt")})
    assert not r2["ok"] and "文件不可读或非代码文件" in r2["error"]


def test_symbol_missing_and_occurrence_bounds(codebase, open_sandbox):
    r = registry.call("ide_read_symbol",
                      {"file": str(codebase / "s.py"), "name": "ghost"})
    assert not r["ok"] and "符号 ghost 不存在——用 ide_outline 查清单" in r["error"]
    r0 = registry.call("ide_read_symbol",
                       {"file": str(codebase / "s.py"), "name": "handle",
                        "occurrence": 0})
    assert not r0["ok"] and "occurrence=0 越界（handle 共 2 处）" in r0["error"]
    r3 = registry.call("ide_read_symbol",
                       {"file": str(codebase / "s.py"), "name": "handle",
                        "occurrence": 3})
    assert not r3["ok"] and "occurrence=3 越界" in r3["error"]
    rn = registry.call("ide_read_symbol",
                       {"file": str(codebase / "s.py"), "name": "handle",
                        "occurrence": -1})
    assert not rn["ok"] and "occurrence=-1 越界" in rn["error"]


def test_empty_path_valueerror_envelope(open_sandbox):
    # 旧实现 _fs_resolve("") 抛 ValueError("path 必填")；薄壳经 exe exit 2 同包络
    r = registry.call("ide_outline", {"file": ""})
    assert not r["ok"] and "path 必填" in r["error"]


# ---------- 沙盒包络 ----------

def test_sandbox_deny_valueerror_envelope(codebase, monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "Z:\\no-such-root-xyz")
    r = registry.call("ide_outline", {"file": str(codebase / "s.py")})
    assert not r["ok"] and "路径越界" in r["error"]


def test_fail_closed_when_unset(codebase, monkeypatch):
    monkeypatch.delenv("UNIFIED_RX_SANDBOX", raising=False)
    r = registry.call("ide_outline", {"file": str(codebase / "s.py")})
    assert not r["ok"]


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    bogus = tmp_path / "not-an-exe.exe"
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", str(bogus))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")
    r = registry.call("ide_outline", {"file": str(tmp_path)})
    assert not r["ok"] and "rx-ide.exe 不存在" in r["error"]


# ---------- 注册面契约 ----------

def test_schemas_unchanged():
    assert registry._TOOLS["ide_outline"]["schema"]["required"] == ["file"]
    assert registry._TOOLS["ide_read_symbol"]["schema"]["required"] == ["file", "name"]
    assert registry._TOOLS["ide_read_symbol"]["schema"]["properties"]["occurrence"]\
        ["type"] == "integer"
    assert registry._TOOLS["ide_outline"]["group"] == "ide"


def test_thin_shell_retirement():
    # S92：行匹配器/装载/params 计数全部退役到 rust/src/ide.rs
    src = open(os.path.join(os.path.dirname(tools.__file__), "ide_read.py"),
               encoding="utf-8").read()
    assert "_symbol_spans(" not in src  # docstring 可提其名，调用必须退役
    assert "from tools.scan" not in src
    assert "_fs_resolve" not in src  # os.path.isfile 保留——exe 定位纪律需要它
    assert '_rx_ide_call(["outline"' in src
    assert '_rx_ide_call(["read_symbol"' in src
    assert 'rx-ide.exe 不存在' in src
