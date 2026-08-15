# -*- coding: utf-8 -*-
"""sage_scan 测试（阶段3：SAGE 式语义回归优先级）。"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sage_scan as ss  # noqa: E402


def _git_repo(tmp_path):
    """造一个带 2 个提交的临时 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "test_mod.py").write_text("def test_f():\n    assert f()\n",
                                      encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"],
                   check=True, capture_output=True)
    # 第二个提交：改 mod.py + 消息含 fix
    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm",
                    "fix: 修复崩溃 panic"], check=True, capture_output=True)
    return str(repo)


def test_semantic_tags():
    tags = ss._semantic_tags(["fix: 修复崩溃 panic", "feat: 新增功能"])
    names = {t["tag"] for t in tags}
    assert "bugfix" in names and "feature" in names
    assert "ui" not in names


def test_sage_scan_basic(tmp_path):
    repo = _git_repo(tmp_path)
    r = ss.sage_scan(repo, commits=1)
    assert r["ok"] is True
    assert r["commits"], "应有提交"
    assert "bugfix" in [t["tag"] for t in r["semantic_tags"]]
    assert "mod.py" in r["changed_files"]
    # 测试映射（启发式兜底至少找到 test_mod）
    paths = [t["test"] for t in r["prioritized_tests"]]
    assert any("test_mod" in p for p in paths), paths


def test_sage_scan_bad_path():
    r = ss.sage_scan("D:/no/such/repo/xyz")
    assert r["ok"] is False


def test_sage_scan_empty_repo(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    r = ss.sage_scan(str(repo), commits=1)
    assert r["ok"] is False
