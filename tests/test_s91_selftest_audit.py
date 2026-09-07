# -*- coding: utf-8 -*-
"""S91 selftest 机器对账契约测试：VERSION_TAG / SKILLS_DOCS 两个对账器。

近期 #2 兑现（84034eb 教训工具化 + S88 手工补契约声明的教训工具化）：
- VERSION_TAG：SERVER_VERSION ↔ 最新 git tag（OK/NEXT/DRIFT/SKIP 四态，
  组件数值序 v2.9.0 < v2.10.0）；
- SKILLS_DOCS：skills/*.md ↔ registry 工具名（零命中文件 = dead，在册外
  域前缀名 = stale）；真实仓库回归门：文档必须与注册表对齐。
"""
import os
import subprocess
from pathlib import Path

import pytest

import server  # noqa: E402


def _git(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True,
                          input=b"")


def _repo_with_tag(tmp_path, *tags):
    _git(["git", "init", "-q"], tmp_path)
    _git(["git", "-c", "user.email=t@t", "-c", "user.name=t",
          "commit", "--allow-empty", "-q", "-m", "init"], tmp_path)
    for t in tags:
        _git(["git", "tag", t], tmp_path)
    return tmp_path


# ---------- VERSION_TAG ----------

def test_latest_tag_sorts_numerically(tmp_path):
    _repo_with_tag(tmp_path, "v2.9.0", "v2.10.0", "v2.2.0")
    assert server._latest_v_tag(str(tmp_path)) == "v2.10.0"


def test_version_tag_ok_when_equal(tmp_path, monkeypatch):
    _repo_with_tag(tmp_path, "v9.9.0")
    monkeypatch.setattr(server, "SERVER_VERSION", "9.9.0")
    assert server._selftest_version_tag(str(tmp_path)) == ("OK", "v9.9.0")


def test_version_tag_next_when_ahead(tmp_path, monkeypatch):
    # 版本领先 = 开发中待发版，正常
    _repo_with_tag(tmp_path, "v9.9.0")
    monkeypatch.setattr(server, "SERVER_VERSION", "10.0.0")
    assert server._selftest_version_tag(str(tmp_path))[0] == "NEXT"


def test_version_tag_drift_when_behind(tmp_path, monkeypatch):
    # 版本落后 = 真实漂移信号（84034eb 教训：serverInfo 停更 18 轮）
    _repo_with_tag(tmp_path, "v9.9.0")
    monkeypatch.setattr(server, "SERVER_VERSION", "0.9.0")
    assert server._selftest_version_tag(str(tmp_path))[0] == "DRIFT"


def test_version_tag_skip_without_tags(tmp_path):
    _repo_with_tag(tmp_path)  # 有仓库无 tag
    assert server._selftest_version_tag(str(tmp_path)) == ("SKIP", "-")
    # 非 git 目录同样 SKIP
    plain = tmp_path / "plain"
    plain.mkdir()
    assert server._selftest_version_tag(str(plain)) == ("SKIP", "-")


def test_selftest_prints_reconciliation_lines(capsys, monkeypatch):
    # 真仓自检：两行对账必须出现，且退出码语义不变（SCHEMA_BAD 0 → 0）
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")
    rc = server.selftest()
    out = capsys.readouterr().out
    assert "VERSION_TAG" in out and "SKILLS_DOCS" in out
    assert rc == 0


# ---------- SKILLS_DOCS ----------

def test_skills_docs_real_repo_aligned():
    # 回归门：真实 skills/*.md 必须与注册表对齐（无 stale、无 dead）
    stale, dead = server._selftest_skills_docs()
    assert stale is not None
    assert stale == [], f"skills 文档陈旧工具名: {stale}"
    assert dead == [], f"skills 文档零工具命中: {dead}"


def test_skills_docs_detects_stale_and_dead(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "skills" / "a.md").write_text(
        "用 fs_read 读文件，另见 fs_ghost_tool（已退役名）\n", encoding="utf-8")
    (tmp_path / "skills" / "b.md").write_text(
        "本文只讲哲学，不提任何在册工具\n", encoding="utf-8")
    # c.md 提到的 ide_edit 是模块名（tools/ide_edit.py 存在）→ 不算 stale
    (tmp_path / "tools" / "ide_edit.py").write_text("# module\n", encoding="utf-8")
    (tmp_path / "skills" / "c.md").write_text(
        "实现在 tools/ide_edit.py，入口用 fs_write\n", encoding="utf-8")
    stale, dead = server._selftest_skills_docs(str(tmp_path))
    assert stale == ["fs_ghost_tool"]
    assert dead == ["b.md"]
