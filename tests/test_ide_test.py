# -*- coding: utf-8 -*-
"""R2：ide_test 统一测试入口。

pytest 走真链路（本解释器自带 pytest）；cargo 走真迷你 crate（编译无依赖，
秒级）；go 条件跳过（机器有 go 才跑）；解析器另配 canned-output 单测。
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools.ide_test import _run_cargo, _run_go  # noqa: E402


def call(name, args):
    args = {**args, "__authorized": True}   # S61 执行类工具统一授权（测试语境）
    r = registry.call(name, args)
    assert r.get("ok"), f"{name}: {r.get('error')}"
    return r["result"]


# ---------- pytest（真链路） ----------

def test_pytest_pass_and_fail(tmp_path):
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_ok.py").write_text(
        "def test_good():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (t / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2, 'two is not one'\n", encoding="utf-8")
    r = call("ide_test", {"path": str(tmp_path)})
    assert r["tool"] == "pytest"
    assert r["passed"] == 1 and r["failed"] == 1
    assert r["failures"] and "test_bad" in r["failures"][0]["test"]
    assert "two is not one" in (r["failures"][0]["msg"] or "")


def test_pytest_zero_collected_is_explicit(tmp_path):
    """核心诚实语义：收集到 0 个测试 ≠ 绿灯，是显式问题。"""
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_empty.py").write_text("x = 1\n", encoding="utf-8")
    r = call("ide_test", {"path": str(tmp_path)})
    assert r["collected"] == 0 and "没写测试" in r["note"]


def test_pytest_no_infra_is_error(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    r = registry.call("ide_test", {"path": str(tmp_path), "__authorized": True})
    assert not r["ok"] and "未检测到测试设施" in r["error"]


def test_target_flag_injection_rejected(tmp_path):
    """S60：target 以 '-' 开头 = argv 旗标注入（--junitxml 等）——必须拒绝。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    for evil in ("--junitxml=C:/Temp/pwn.xml", "-x", "-p", "--collect-only"):
        r = registry.call("ide_test", {"path": str(tmp_path), "target": evil,
                                       "__authorized": True})
        assert not r["ok"] and "argv" in r["error"], evil


# ---------- cargo（真迷你 crate） ----------

def test_cargo_real_minimal_crate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "uRX_t"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text(
        "#[test]\nfn good() { assert_eq!(2 + 2, 4); }\n"
        "#[test]\nfn bad() { assert_eq!(1, 2, \"one not two\"); }\n",
        encoding="utf-8")
    r = call("ide_test", {"path": str(tmp_path)})
    assert r["tool"] == "cargo"
    assert r["passed"] == 1 and r["failed"] == 1
    assert any("bad" in f["test"] for f in r["failures"])
    assert "one not two" in (r.get("panic") or ""), \
        "cargo 失败必须带 panic 消息（回喂信号）"


# ---------- 解析器（canned output，不依赖工具链） ----------

def test_cargo_parser_canned(monkeypatch):
    out = ("test core::ok ... ok\n"
           "test core::bad ... FAILED\n"
           "thread 'core::bad' panicked at src/lib.rs:7:5:\n"
           "assertion failed\n"
           "  at src/inner.rs:3:9\n"
           "test result: FAILED. 1 passed; 1 failed; 2 ignored\n")
    monkeypatch.setattr("tools.ide_test._exec",
                        lambda *a, **k: {"code": 101, "stdout": out, "stderr": ""})
    r = _run_cargo("p", "p", None, 60)
    assert r["passed"] == 1 and r["failed"] == 1 and r["skipped"] == 2
    assert r["failures"][0]["test"] == "core::bad"
    assert r["panic"] and r["panic_at"] == "src/lib.rs:7"
    assert r["frames"] and r["frames"][0]["line"] == 3


def test_cargo_parser_multi_crate_workspace(monkeypatch):
    """S63：workspace 多 crate → 多条 test result 行必须全量累加
    （VF3 实测 177 测试只报 52 的根因）。"""
    out = ("test a::ok ... ok\n"
           "test result: ok. 100 passed; 0 failed; 0 ignored\n"
           "\n"
           "test b::bad ... FAILED\n"
           "test result: FAILED. 77 passed; 3 failed; 2 ignored\n")
    monkeypatch.setattr("tools.ide_test._exec",
                        lambda *a, **k: {"code": 101, "stdout": out, "stderr": ""})
    r = _run_cargo("p", "p", None, 60)
    assert r["passed"] == 177 and r["failed"] == 3 and r["skipped"] == 2
    assert r["result_lines"] == 2
    assert any("b::bad" == f["test"] for f in r["failures"])


def test_go_parser_canned(monkeypatch):
    out = ("=== RUN   TestA\n"
           "--- PASS: TestA (0.00s)\n"
           "--- FAIL: TestB (0.01s)\n"
           "--- SKIP: TestC (0.00s)\n"
           "FAIL\n")
    monkeypatch.setattr("tools.ide_test._exec",
                        lambda *a, **k: {"code": 1, "stdout": out, "stderr": ""})
    r = _run_go("p", "p", None, 60)
    assert r["passed"] == 1 and r["failed"] == 1 and r["skipped"] == 1
    assert r["failures"][0]["test"] == "TestB"


def test_go_real_if_present(tmp_path):
    if not shutil.which("go"):
        return  # 机器无 go → 如实跳过（不假装测过）
    (tmp_path / "go.mod").write_text(
        "module urxtest\n\ngo 1.21\n", encoding="utf-8")
    (tmp_path / "main_test.go").write_text(
        "package main\n\nimport \"testing\"\n\n"
        "func TestGood(t *testing.T) {}\n"
        "func TestBad(t *testing.T) { t.Fatal(\"nope\") }\n",
        encoding="utf-8")
    r = call("ide_test", {"path": str(tmp_path)})
    assert r["failed"] == 1 and any("TestBad" in f["test"] for f in r["failures"])
