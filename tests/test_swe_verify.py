# -*- coding: utf-8 -*-
"""S24 swe_verify 离线回归：FTB 标签转换 / sympy 裸名定位 / pull 幂等。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import swe_verify as sv  # noqa: E402


# ---------- django 括号标签 → runtests 标签 ----------

def test_django_labels_class_only():
    assert sv._django_labels(
        ["test_overriding_FIELD_display (model_fields.tests.GetFieldDisplayTests)"]
    ) == ["model_fields.tests.GetFieldDisplayTests.test_overriding_FIELD_display"]


def test_django_labels_full_path_dedup():
    assert sv._django_labels(
        ["test_filter_multiple (xor_lookups.tests.XorLookupsTests.test_filter_multiple)"]
    ) == ["xor_lookups.tests.XorLookupsTests.test_filter_multiple"]


def test_django_labels_sentence_is_unparseable():
    assert sv._django_labels(
        ["The prefetched relationship is used rather than populating the reverse"]) == []
    assert sv._django_labels([]) == []


# ---------- sympy 裸 test 名定位 ----------

def test_sympy_resolve_finds_def(tmp_path):
    d = tmp_path / "sympy" / "sets" / "tests"
    d.mkdir(parents=True)
    (d / "test_sets.py").write_text("def test_issue_12420():\n    pass\n",
                                    encoding="utf-8")
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "test_sets.py").write_text("def test_issue_12420():\n    pass\n",
                                                   encoding="utf-8")
    hits = sv._sympy_resolve(str(tmp_path), ["test_issue_12420"])
    assert hits == [os.path.join("sympy", "sets", "tests", "test_sets.py")]


def test_sympy_resolve_missing_returns_empty(tmp_path):
    assert sv._sympy_resolve(str(tmp_path), ["no_such_test"]) == []


# ---------- pull 幂等（不重复加字段、不丢任务） ----------

def test_pull_merges_once(tmp_path, monkeypatch):
    sample = tmp_path / "sample.jsonl"
    rec = {"instance_id": "x__y-1", "repo": "x/y", "base_commit": "c" * 40,
           "issue": "i", "gold_patch": "p"}
    sample.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    parquet_rows = [("x__y-1", "test patch diff",
                     json.dumps(["t1"]), json.dumps(["p1", "p2"]))]
    monkeypatch.setattr(sv, "SAMPLE", str(sample))
    monkeypatch.setattr(sv, "PARQUET", "fake.parquet")

    import duckdb
    class FakeCon:
        def execute(self, q):
            return self
        def fetchall(self):
            return parquet_rows
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: FakeCon())

    sv.pull()
    d1 = json.loads(sample.read_text(encoding="utf-8").splitlines()[0])
    assert d1["test_patch"] == "test patch diff"
    assert d1["ftb"] == ["t1"] and d1["ptb"] == ["p1", "p2"]
    sv.pull()                                   # 幂等：已有字段不重写
    d2 = json.loads(sample.read_text(encoding="utf-8").splitlines()[0])
    assert d2 == d1


# ---------- verify_one 的候选分支（fake 环境，零 API） ----------

def test_verify_one_no_candidate(tmp_path, monkeypatch):
    import subprocess
    calls = []
    root = tmp_path / "x__y-1"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text("line1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    patch = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
             "@@ -1 +1,2 @@\n line1\n+line2\n")
    monkeypatch.setattr(sv, "_restore", lambda r: calls.append("restore"))
    monkeypatch.setattr(sv, "_run_tests", lambda *a, **k: (1, "base fails"))
    rec = {"instance_id": "x__y-1", "mech": {"candidate_diff": ""}}
    inst = {"test_patch": patch, "ftb": ["t"], "ptb": [], "repo": "x/y"}
    monkeypatch.setattr(sv, "WORK", str(tmp_path))
    v = sv.verify_one(rec, inst, "python", {})
    assert calls == ["restore"]
    assert v["base_ftb_fail"] is True
    assert v["verified"] is False and v["why"] == "no-candidate-diff"
