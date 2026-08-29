# -*- coding: utf-8 -*-
"""S55：ide_common 共享解析器直接单测（此前只被 registry 间接路过）。

真实断言对象：行尾检测 / 语言映射 / 沙盒读 / 遍历上限 / gcc-cargo-go 诊断解析。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools  # noqa: E402,F401
from tools.ide_common import (_detect_eol, _iter_files, _lang_of, _parse_cargo_short,
                              _parse_gcc, _parse_go_build, _read)


# ---------- 行尾 / 语言 ----------

def test_detect_eol():
    assert _detect_eol("a\r\nb\r\n") == "\r\n"
    assert _detect_eol("a\nb\n") == "\n"
    assert _detect_eol("a\r\nb\r\nc\n") == "\r\n"   # 2:1 取多数
    assert _detect_eol("a\r\nb\n") == "\n"          # 平票从简 → LF（钉死行为）


def test_lang_of():
    assert _lang_of("x.py") == "python"
    assert _lang_of("x.RS") == "rust"
    assert _lang_of("x.tsx") == "typescript"
    assert _lang_of("x.txt") == "text"


# ---------- _read 沙盒语义 ----------

def test_read_ok_and_reject(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("hello\n", encoding="utf-8", newline="")
    assert _read(str(f)) == "hello\n"
    assert _read(str(tmp_path / "missing.py")) is None
    # 沙盒外 → 拒（读不到 = None，不抛穿）
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    assert _read(os.path.join(ROOT, "registry.py")) is None


# ---------- _iter_files ----------

def test_iter_files_skip_and_cap(tmp_path):
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "b.rs").write_text("fn m(){}\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("not code\n", encoding="utf-8")
    skip = tmp_path / "node_modules"
    skip.mkdir()
    (skip / "d.js").write_text("v=1\n", encoding="utf-8")
    got = list(_iter_files(str(tmp_path), 10))
    names = {os.path.basename(p) for p in got}
    assert names == {"a.py", "b.rs"}
    got1 = list(_iter_files(str(tmp_path), 1))
    assert len(got1) == 1


# ---------- 诊断解析器（gcc / cargo / go） ----------

def test_parse_gcc():
    out = ('src/main.c:10:5: error: expected \';\' before return\n'
           'src/main.c:10:5: error: expected \';\' before return\n'   # 去重
           'src/util.c:3:1: warning: unused variable x\n')
    diags = _parse_gcc(out, ROOT)
    assert len(diags) == 2
    d0 = diags[0]
    assert d0["line"] == 10 and d0["col"] == 5 and d0["level"] == "error"
    assert "expected" in d0["msg"]
    assert diags[1]["level"] == "warning"


def test_parse_cargo_short():
    out = ('src\\lib.rs:3:7: error[E0382]: borrow of moved value: `v`\n'
           'src\\main.rs:12:1: warning: unused import\n')
    diags = _parse_cargo_short(out, ROOT)
    assert len(diags) == 2
    assert diags[0]["line"] == 3 and diags[0]["col"] == 7
    assert diags[0]["level"] == "error" and "borrow" in diags[0]["msg"]


def test_parse_go_build():
    out = ("./main.go:9:5: undefined: foo\n"
           "./util.go:2:1: too many arguments\n")
    diags = _parse_go_build(out, ROOT)
    assert len(diags) == 2
    assert all(d["level"] == "error" for d in diags)   # go build 无 level 词 → 全 error
    assert diags[0]["line"] == 9 and diags[0]["col"] == 5
