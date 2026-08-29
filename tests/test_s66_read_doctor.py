# -*- coding: utf-8 -*-
"""S66：IDE 升级回归钉——ide_outline / ide_read_symbol / ide_doctor diff 模式。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

RUST_SRC = """pub fn alpha(a: u32, b: u32) -> u32 {
    a + b
}

fn helper() {
    let x = 1;
    let y = 2;
}

pub(crate) fn beta() -> &'static str {
    "b"
}
"""

PY_SRC = """def outer(p, q):
    def inner():
        return 1

    return p + q


class Thing:
    def method(self):
        return 2
"""


# ---------- ide_outline ----------

def test_outline_rust_spans(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text(RUST_SRC, encoding="utf-8")
    r = registry.call("ide_outline", {"file": str(f)})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["lang"] == "rust" and res["total"] == 3
    names = [s["name"] for s in res["symbols"]]
    assert names == ["alpha", "helper", "beta"], names
    alpha = res["symbols"][0]
    assert alpha["line"] == 1 and alpha["end_line"] == 3 and alpha["params"] == 2


def test_outline_python_nested(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(PY_SRC, encoding="utf-8")
    r = registry.call("ide_outline", {"file": str(f)})
    res = r["result"]
    names = [s["name"] for s in res["symbols"]]
    assert "outer" in names and "inner" in names and "method" in names
    method = next(s for s in res["symbols"] if s["name"] == "method")
    assert method["params"] == 1


def test_outline_errors(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("no code\n", encoding="utf-8")
    r = registry.call("ide_outline", {"file": str(f)})
    assert not r["ok"]
    r2 = registry.call("ide_outline", {"file": str(tmp_path / "ghost.py")})
    assert not r2["ok"]


# ---------- ide_read_symbol ----------

def test_read_symbol_exact_body_and_occurrence(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text(RUST_SRC, encoding="utf-8")
    r = registry.call("ide_read_symbol", {"file": str(f), "name": "alpha"})
    res = r["result"]
    assert res["start"] == 1 and res["end"] == 3 and res["params"] == 2
    assert "a + b" in res["content"] and "}" in res["content"]


def test_read_symbol_missing_and_occurrence_bounds(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(PY_SRC, encoding="utf-8")
    r = registry.call("ide_read_symbol", {"file": str(f), "name": "nope"})
    assert not r["ok"] and "ide_outline" in r["error"]
    r2 = registry.call("ide_read_symbol", {"file": str(f), "name": "outer",
                                           "occurrence": 2})
    assert not r2["ok"] and "越界" in r2["error"]


# ---------- ide_doctor diff 模式 ----------

def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def test_doctor_diff_mode_flags_only_changes(tmp_path):
    (tmp_path / "stable.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "touched.py").write_text("y = 1\n", encoding="utf-8")
    _git_init(tmp_path)
    # 基线后：touched.py 引入 definite bug（裸 eval），stable.py 不动
    (tmp_path / "touched.py").write_text(
        "def f(u):\n    return eval(u)\n", encoding="utf-8")
    # 未跟踪新文件（也应纳入 diff 体检）
    (tmp_path / "new.py").write_text("z = 1\n", encoding="utf-8")
    r = registry.call("ide_doctor", {"path": str(tmp_path), "diff": True,
                                     "__authorized": True})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["diff"] is True
    names = {c["check"] for c in res["checks"]}
    assert "dep_graph" not in names and "stability" not in names, \
        "diff 模式跳过全仓检查"
    # 改动文件上的 definite bug 必须进 problems；未动文件的干净不产生噪音
    assert res["verdict"] == "issues", res
    assert any("definite" in p for p in res["problems"]), res["problems"]


def test_doctor_diff_requires_git(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ide_doctor", {"path": str(tmp_path), "diff": True,
                                     "__authorized": True})
    assert not r["ok"] and "git" in r["error"]
