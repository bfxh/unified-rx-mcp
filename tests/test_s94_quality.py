# -*- coding: utf-8 -*-
"""S94 质量体检测试：exe 版本对账机器（--version 门 × selftest EXE_TAG）+
版本锁步静态守卫 + bench 脚本纯函数抽测。

背景（用户：「你不更新某一个东西当然出问题」——exe 旧的、代码新的，此前
没有任何机器对账能发现）：S94 给 9 个 rust bin 加 --version
（CARGO_PKG_VERSION 编译期注入），selftest 增 EXE_TAG 行逐个比对
SERVER_VERSION。本文件锁「对账机器本身」；预算复测数字机器相关、不进
pytest——数字走 bench/s94_perf.py 留档 bench/results/s94_perf.json。
"""
import importlib.util
import os
import re

import pytest

import server
import tools  # noqa: F401
from tools import appaudit, astscan, fs, ide_read, scan, search

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_BIN_DIR = os.path.join(_ROOT, "rust", "src", "bin")


def _bin_paths():
    return sorted(os.path.join(_BIN_DIR, f) for f in os.listdir(_BIN_DIR)
                  if f.startswith("rx_") and f.endswith(".rs"))


def test_exe_names_cover_all_nine_bins():
    """_RX_EXE_NAMES ↔ rust/src/bin/rx_*.rs 一一对应：新增 bin 忘登记即红。"""
    stems = {os.path.splitext(os.path.basename(p))[0] for p in _bin_paths()}
    # bin 源文件用下划线（rx_mcp.rs），exe 用连字符（rx-mcp.exe）
    expect = {n[:-4].replace("-", "_") for n in server._RX_EXE_NAMES}
    assert len(server._RX_EXE_NAMES) == 9
    assert stems == expect


def test_exe_names_match_tool_locators():
    """对账名单 ↔ 各工具层定位常量交叉对账：改名漂移即红。"""
    assert fs._RX_EXE_NAME in server._RX_EXE_NAMES
    assert astscan._RX_EXE_NAME in server._RX_EXE_NAMES
    assert scan._RX_SCAN_EXE_NAME in server._RX_EXE_NAMES
    assert ide_read._RX_IDE_EXE_NAME in server._RX_EXE_NAMES
    assert search._RX_EXE_NAME in server._RX_EXE_NAMES
    assert search._SEM_EXE_NAME in server._RX_EXE_NAMES
    assert appaudit._RX_AUDIT_EXE_NAME in server._RX_EXE_NAMES
    assert appaudit._RX_APPOPS_EXE_NAME in server._RX_EXE_NAMES
    assert "rx-taint.exe" in server._RX_EXE_NAMES


def test_all_bins_have_version_gate():
    """每个 bin 源码含 --version 门（CARGO_PKG_VERSION 编译期注入）。"""
    for p in _bin_paths():
        with open(p, encoding="utf-8") as f:
            src = f.read()
        assert 'a == "--version"' in src, f"{p} 缺 --version 门"
        assert "CARGO_PKG_VERSION" in src, f"{p} 缺版本注入"


def test_exe_tag_skip_when_all_missing(tmp_path, monkeypatch):
    """9 个全缺 → None（SKIP：纯 Python 环境未 cargo build，不算漂移）。"""
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("UNIFIED_RX_RS_EXE", raising=False)
    assert server._selftest_exe_tag() is None


def test_exe_tag_drift_on_junk_exe(tmp_path, monkeypatch):
    """伪 exe（存在但跑不出版本号）→ 计 drift 不计 missing；细节带名字。"""
    rel = tmp_path / "rx-rs-target" / "release"
    rel.mkdir(parents=True)
    (rel / "rx-fs.exe").write_bytes(b"junk-not-an-exe")
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("UNIFIED_RX_RS_EXE", raising=False)
    r = server._selftest_exe_tag()
    assert r is not None
    ok, drift, missing = r
    assert ok == 0
    assert len(drift) == 1 and drift[0].startswith("rx-fs.exe(")
    assert server.SERVER_VERSION not in drift[0]
    assert len(missing) == 8


def test_exe_tag_ok_with_real_exes():
    """真机（有 %TEMP%\\rx-rs-target/release 编译产物）→ ok=9 drift=0。"""
    rel = os.path.join(os.environ.get("TEMP", ""), "rx-rs-target", "release",
                       "rx-fs.exe")
    if not os.path.isfile(rel):
        pytest.skip("本机未 cargo build（无 rx-rs-target 产物）")
    assert server._selftest_exe_tag() == (9, [], [])


def test_selftest_prints_exe_tag(capsys):
    """selftest 汇总面出现 EXE_TAG 行（真机 ok=9 / 裸环境 SKIP）。"""
    assert server.selftest() == 0
    out = capsys.readouterr().out
    assert "EXE_TAG ok=" in out or "EXE_TAG SKIP" in out


def test_version_lockstep_server_equals_cargo_toml():
    """SERVER_VERSION ↔ rust/Cargo.toml [package].version 锁步（S91 84034eb
    教训延伸：不只 serverInfo，exe 注入版本也必须同步，漂移机器抓）。"""
    with open(os.path.join(_ROOT, "rust", "Cargo.toml"),
              encoding="utf-8") as f:
        m = re.search(r'^version = "([^"]+)"', f.read(), re.M)
    assert m, "Cargo.toml 缺 [package].version"
    assert server.SERVER_VERSION == m.group(1)


# ---- bench/s94_perf.py 纯函数抽测（不做机器相关断言） ----

_spec = importlib.util.spec_from_file_location(
    "s94_perf", os.path.join(_ROOT, "bench", "s94_perf.py"))
s94_perf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s94_perf)


def test_budget_table_covers_eval_l2():
    """bench 门禁表 ↔ EVAL L2 预算行三件齐全。"""
    assert set(s94_perf._BUDGETS) == {"fs_stat", "ast_scan", "engine_query"}


def test_timed_stats_shape():
    """_timed 返回 min/p50/max/n 且 min ≤ p50 ≤ max。"""
    r = s94_perf._timed(lambda: None, 7)
    assert set(r) == {"min", "p50", "max", "n"} and r["n"] == 7
    assert 0 <= r["min"] <= r["p50"] <= r["max"]


def test_rss_bytes_returns_int_or_none():
    """rss_bytes：本机应返回正整数；不可用平台返回 None（脚本自行跳过）。"""
    v = s94_perf.rss_bytes()
    assert v is None or (isinstance(v, int) and v > 0)
