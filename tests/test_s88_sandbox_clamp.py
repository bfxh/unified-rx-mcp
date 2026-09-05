# -*- coding: utf-8 -*-
"""S88 回归：scan/search/game/ops 读取面沙盒钳制（S73 纪律补全）+ junction 逃逸回归。

S88 三路排查（Mimosa 副本深扫 + attack 域五件套 + 人工精读）实锤：scan 域
bug_scan/std_check/ui_check/bug_locate/project_scan、search 域 code_search/
code_semantic、game 域 game_check、ops 域 project_health 吃任意路径却未过
沙盒——与 code_review/dep_graph/backup 的 S73"读路径同样过沙盒"纪律不一致。
本文件钉死：越界一律 {"error": 路径越界（沙盒外）}；沙盒内照常工作；
junction 穿越读（realpath 解析到沙盒外）拒绝——3.14 junction≠symlink 语义
下的实证探针固化为回归（S88 attack_run.py 实测 three 连全拒，此处防退化）。
"""
import os
import subprocess

import pytest

from registry import call as rx_call
from tools import scan as scan_tools
from tools import search as search_tools
from tools import game as game_tools
from tools import ops as ops_tools

# conftest 沙盒 = 仓库根 + %TEMP%\unified-rx-pytest；C:\Windows 恒在沙盒外
OUTSIDE = r"C:\Windows"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def cwd_in_repo(monkeypatch):
    # 默认 cwd 语义测试：把 os.getcwd 钉在仓库根（conftest 沙盒内），
    # 使测试不依赖 pytest 的启动目录（从外部 cwd 跑也稳定）
    monkeypatch.setattr(os, "getcwd", lambda: REPO_ROOT)


def _assert_refused(r):
    assert isinstance(r, dict) and r.get("error"), r
    assert "沙盒外" in str(r["error"]), r


# ---------- scan 域（4 工具 + 组合） ----------

def test_scan_trio_outside_refused():
    for fn in (scan_tools.bug_scan, scan_tools.std_check, scan_tools.ui_check):
        _assert_refused(fn(OUTSIDE))


def test_bug_locate_outside_root_refused():
    _assert_refused(scan_tools.bug_locate("ValueError: x", root=OUTSIDE))


def test_bug_locate_default_cwd_not_sandbox_refusal(cwd_in_repo):
    # cwd=仓库根在 conftest 沙盒内 → 不得报沙盒外（exe 缺失走存在性错误，另行报）
    r = scan_tools.bug_locate("no such error text zzz_s88")
    assert "沙盒外" not in str(r.get("error", "")), r


def test_project_scan_outside_refused():
    _assert_refused(scan_tools.project_scan(OUTSIDE))


def test_bug_scan_inside_sandbox_works(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    r = scan_tools.bug_scan(str(tmp_path), max_files=10)
    assert "沙盒外" not in str(r.get("error", "")), r


# ---------- search 域 ----------

def test_search_domain_outside_refused():
    for fn in (search_tools.code_search, search_tools.code_semantic):
        _assert_refused(fn("query", root=OUTSIDE))


def test_search_default_cwd_not_sandbox_refusal(cwd_in_repo):
    r = search_tools.code_search("zzz_no_match_s88")
    assert "沙盒外" not in str(r.get("error", "")), r


# ---------- game / ops ----------

def test_game_check_outside_refused():
    _assert_refused(game_tools.game_check(OUTSIDE))


def test_project_health_outside_refused_not_full_score():
    # 修复动机：越界错误曾被吞成 0 问题 → 满分；现在必须报错且不给 score
    r = ops_tools.project_health(OUTSIDE)
    _assert_refused(r)
    assert "score" not in r, r


# ---------- junction 逃逸回归（S88 实证探针固化） ----------

@pytest.mark.skipif(os.name != "nt", reason="Windows junction 语义")
def test_fs_read_through_junction_refused(tmp_path):
    target = r"C:\Windows"
    junc = tmp_path / "j_win"
    mk = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), target],
                        capture_output=True)
    if not os.path.lexists(str(junc)):
        pytest.skip(f"junction 创建失败 rc={mk.returncode}")
    try:
        r = rx_call("fs_read", {"path": str(junc / "explorer.exe")})
        assert not r.get("ok"), r
        assert "越界" in str(r.get("error", "")), r
    finally:
        try:
            os.rmdir(str(junc))   # 只删链接本身，不动目标
        except OSError:
            pass


# ---------- 门面巡检（S88 改动不得碰门） ----------

def test_auth_gate_sweep_still_ok():
    r = rx_call("auth_gate_sweep", {})
    assert r.get("ok") and (r.get("result") or {}).get("ok"), r


def test_path_probe_still_all_safe():
    r = rx_call("path_probe", {})
    assert r.get("ok") and (r.get("result") or {}).get("all_safe"), r
