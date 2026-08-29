# -*- coding: utf-8 -*-
"""R4：ide_doctor 一键体检。

真链路聚合测试：git 化 tmp 项目（通过 py 测试）→ 六个检查全在场 + verdict；
坏项目（语法错误 / 测试失败）→ problems 命中 + verdict=issues。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def _make_project(tmp_path, body="def add(a, b):\n    return a + b\n",
                  test_body="from pkg_under_test import add\n\n"
                            "def test_add():\n    assert add(1, 2) == 3\n"):
    pkg = tmp_path / "pkg_under_test.py"
    pkg.write_text(body, encoding="utf-8")
    t = tmp_path / "tests"
    t.mkdir(exist_ok=True)
    (t / "test_it.py").write_text(test_body, encoding="utf-8")
    _git_init(tmp_path)
    return str(tmp_path)


def test_doctor_green_project(tmp_path):
    p = _make_project(tmp_path)
    r = registry.call("ide_doctor", {"path": p})
    assert r["ok"], r.get("error")
    res = r["result"]
    names = {c["check"] for c in res["checks"]}
    assert names == {"bug_scan", "code_review", "build", "test",
                     "dep_graph", "stability"}
    assert all(c["status"] == "ok" for c in res["checks"]), res["checks"]
    assert res["verdict"] == "clean", res["problems"]
    assert res["elapsed_s"] > 0


def test_doctor_flags_broken_build(tmp_path):
    p = _make_project(tmp_path, body="def broken(:\n    return 1\n")
    r = registry.call("ide_doctor", {"path": p})
    res = r["result"]
    assert res["verdict"] == "issues"
    assert any("build" in x for x in res["problems"]), res["problems"]


def test_doctor_flags_failing_test(tmp_path):
    p = _make_project(tmp_path, test_body="from pkg_under_test import add\n\n"
                                          "def test_add():\n"
                                          "    assert add(1, 2) == 4\n")
    r = registry.call("ide_doctor", {"path": p})
    res = r["result"]
    assert res["verdict"] == "issues"
    assert any("test failed" in x for x in res["problems"])


def test_doctor_warns_on_missing_tests(tmp_path):
    # 有代码无测试设施 → test 检查黄灯，不是绿灯也不是崩
    p = tmp_path / "solo"
    p.mkdir()
    (p / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(p)
    r = registry.call("ide_doctor", {"path": str(p)})
    res = r["result"]
    assert res["verdict"] in ("warn", "issues")
    assert any("没写测试" in x or "未检测到测试设施" in x for x in res["warns"])


def test_doctor_error_paths(tmp_path):
    r = registry.call("ide_doctor", {"path": str(tmp_path / "ghost")})
    assert not r["ok"]
