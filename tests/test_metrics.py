# -*- coding: utf-8 -*-
"""S55：metrics 域测试（code_coverage / dep_graph / module_stability）。

module_stability 用真 git 仓库验证评分规则（risky/fair 判定 + has_test 三路信号：
专用测试文件名 / 测试内容引用模块名 / 测试内容引用注册工具名）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401  注册全部


def call(name, args):
    r = registry.call(name, args)
    assert r.get("ok"), f"{name} 失败: {r.get('error')}"
    return r["result"]


# ---------- code_coverage ----------

def test_code_coverage_runs_and_measures(tmp_path):
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "util.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef never():\n    return -1\n",
        encoding="utf-8")
    (proj / "run.py").write_text(
        "from pkg.util import add\nprint(add(1, 2))\n", encoding="utf-8")
    r = call("code_coverage", {"script": str(proj / "run.py"),
                               "source_dir": str(proj)})
    assert r["exit"] == 0
    assert 0 < r["coverage_pct"] < 100
    util = [f for f in r["per_file"] if f["file"].endswith("util.py")]
    assert util and 0 < util[0]["pct"] < 100 and util[0]["covered"] >= 2


def test_code_coverage_error_paths(tmp_path):
    r = registry.call("code_coverage", {"script": str(tmp_path / "nope.py"),
                                        "source_dir": str(tmp_path)})
    assert not r["ok"] and "不存在" in r["error"]
    r2 = registry.call("code_coverage", {"script": __file__,
                                         "source_dir": str(tmp_path / "nodir")})
    assert not r2["ok"]


# ---------- dep_graph ----------

def test_dep_graph_cycles_and_internal_external(tmp_path):
    (tmp_path / "a_mod.py").write_text("import b_mod\nimport json\n",
                                       encoding="utf-8")
    (tmp_path / "b_mod.py").write_text("import c_mod\n", encoding="utf-8")
    (tmp_path / "c_mod.py").write_text("import a_mod\n", encoding="utf-8")
    r = call("dep_graph", {"path": str(tmp_path)})
    assert r["total_files"] == 3
    assert r["cycles"], "a→b→c→a 循环必须被发现"
    assert r["internal_deps"] == 3 and r["external_deps"] == 1
    assert "json" in r["external_summary"]
    assert r["graph"]["a_mod.py"] == ["b_mod"]


def test_dep_graph_error(tmp_path):
    r = registry.call("dep_graph", {"path": str(tmp_path / "nope")})
    assert not r["ok"]


# ---------- module_stability ----------

def _git_commit(repo):
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def test_module_stability_scoring_rules(tmp_path):
    (tmp_path / "f_untested.py").write_text(
        "\n".join(f"x{i} = {i}" for i in range(60)) + "\n", encoding="utf-8")
    (tmp_path / "f_tested.py").write_text("y = 1\n", encoding="utf-8")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_f_tested.py").write_text("import f_tested\n", encoding="utf-8")
    # 工具名引用信号：模块注册的工具名出现在测试内容 → has_test（无同名测试文件）
    (tmp_path / "g_tool.py").write_text(
        '@tool("fake_widget_probe", "d", "misc")\n'
        'def fake_widget_probe():\n    return {}\n', encoding="utf-8")
    (tdir / "test_g.py").write_text(
        'FAKE = "fake_widget_probe"\n', encoding="utf-8")
    _git_commit(tmp_path)

    r = call("module_stability", {"path": str(tmp_path)})
    by_mod = {m["module"]: m for m in r["modules"]}
    un = by_mod["f_untested.py"]
    assert un["stability"] == "risky" and not un["has_test"]
    assert "f_untested.py" in r["risky_modules"]
    te = by_mod["f_tested.py"]
    assert te["has_test"] and te["stability"] == "fair"
    gt = by_mod["g_tool.py"]
    assert gt["has_test"] and gt["stability"] == "fair"


def test_module_stability_requires_git(tmp_path):
    r = registry.call("module_stability", {"path": str(tmp_path)})
    assert not r["ok"] and "git" in r["error"]
