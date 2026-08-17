#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_repo_health.py — repo_health 四理念检测测试。

覆盖：去重（完全相同/近似/代码块）、剔残缺（空实现/TODO/断引用）、
分支（非 git 降级 + 真实 git 仓库）、标矛盾（同名符号）、all 汇总评分。
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import repo_health as rh  # noqa: E402


@pytest.fixture()
def tmp_root():
    d = tempfile.mkdtemp(prefix="rh_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


# ── dedup ──────────────────────────────────────────────────────────────

def test_dedup_identical_files(tmp_root):
    code = "def f():\n    return 42\n"
    _write(tmp_root, "a.py", code)
    _write(tmp_root, "b.py", code)
    items = rh.dedup_scan(tmp_root)
    hits = [i for i in items if i.kind == "dedup" and i.severity == "high"]
    assert len(hits) == 1, items
    assert len(hits[0].detail["duplicates"]) == 1


def test_dedup_similar_files(tmp_root):
    # 100 行不同内容，m2 只多 2 行 → 相似度 ≈ 100/102 ≈ 0.98
    base = "".join(f"def fn_{i}(x):\n    return x + {i}\n" for i in range(100))
    _write(tmp_root, "m1.py", base)
    _write(tmp_root, "m2.py", base + "def extra():\n    return 999\n")
    items = rh.dedup_scan(tmp_root)
    assert any(i.severity == "medium" for i in items), items


def test_dedup_excludes_node_modules(tmp_root):
    _write(tmp_root, "node_modules/pkg/index.js", "export const a = 1;\n")
    _write(tmp_root, "src/index.js", "export const a = 1;\n")
    items = rh.dedup_scan(tmp_root)
    assert not any("node_modules" in i.path for i in items)


# ── incomplete ─────────────────────────────────────────────────────────

def test_incomplete_pass_and_not_implemented(tmp_root):
    _write(tmp_root, "svc.py", (
        "class Service:\n"
        "    def run(self):\n"
        "        pass\n"
        "    def stop(self):\n"
        "        raise NotImplementedError\n"))
    items = rh.incomplete_scan(tmp_root)
    msgs = [i.message for i in items]
    assert any("pass" in m for m in msgs), items
    assert any("NotImplementedError" in m for m in msgs), items
    assert all(i.severity == "high" for i in items)


def test_incomplete_todo(tmp_root):
    _write(tmp_root, "x.py", "def f():\n    # TODO: finish later\n    return 1\n")
    items = rh.incomplete_scan(tmp_root)
    assert any("TODO" in i.message for i in items)


def test_incomplete_broken_import(tmp_root):
    _write(tmp_root, "broken.py", "import definitely_not_a_real_module_xyz_123\n")
    items = rh.incomplete_scan(tmp_root)
    assert any("断引用" in i.message for i in items), items


# ── branch ─────────────────────────────────────────────────────────────

def test_branch_non_git_degrade(tmp_root):
    _write(tmp_root, "a.py", "x = 1\n")
    items = rh.branch_scan(tmp_root)
    assert any(i.kind == "branch" and i.severity == "info" and "非 git" in i.message
               for i in items)


def test_branch_real_git_unmerged(tmp_root):
    subprocess.run(["git", "init", "-q", tmp_root], check=True,
                   capture_output=True)
    _write(tmp_root, "main.py", "print('hi')\n")
    subprocess.run(["git", "-C", tmp_root, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp_root, "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp_root, "checkout", "-q", "-b", "feature"],
                   check=True, capture_output=True)
    _write(tmp_root, "feature.py", "print('feature')\n")
    subprocess.run(["git", "-C", tmp_root, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp_root, "commit", "-q", "-m", "feat"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp_root, "checkout", "-q", "master"],
                   check=True, capture_output=True)
    items = rh.branch_scan(tmp_root)
    assert any("未合并分支" in i.message for i in items), items


# ── conflict ───────────────────────────────────────────────────────────

def test_conflict_duplicate_symbol(tmp_root):
    _write(tmp_root, "one.py", "def process():\n    return 1\n")
    _write(tmp_root, "two.py", "def process():\n    return 2\n")
    items = rh.conflict_scan(tmp_root)
    assert any("同名符号" in i.message for i in items), items


def test_conflict_ignores_test_files(tmp_root):
    _write(tmp_root, "impl.py", "def helper():\n    return 1\n")
    _write(tmp_root, "test_impl.py", "def helper():\n    return 2\n")
    items = rh.conflict_scan(tmp_root)
    assert not any("同名符号" in i.message for i in items), items


# ── all ────────────────────────────────────────────────────────────────

def test_repo_health_all(tmp_root):
    _write(tmp_root, "dup_a.py", "def f():\n    return 1\n")
    _write(tmp_root, "dup_b.py", "def f():\n    return 1\n")
    _write(tmp_root, "stub.py", "def g():\n    pass\n")
    r = rh.repo_health("all", tmp_root)
    assert r["ok"] is True
    assert r["action"] == "all"
    assert r["score"] < 100  # 有发现则扣分
    assert "dedup" in r["summary"] or "incomplete" in r["summary"]


def test_repo_health_unknown_action(tmp_root):
    r = rh.repo_health("nope", tmp_root)
    assert r["ok"] is False


def test_repo_health_missing_root():
    r = rh.repo_health("all", r"C:\definitely\missing\dir\xyz")
    assert r["ok"] is False
