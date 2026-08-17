#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_backup_core.py — 每日备份/回溯测试。"""
import glob
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import backup_core as bc  # noqa: E402


def _cleanup(root):
    shutil.rmtree(root, ignore_errors=True)
    for p in glob.glob(os.path.join(bc.BACKUP_ROOT, os.path.basename(root) + "*")):
        shutil.rmtree(p, ignore_errors=True)


def test_daily_backup_excludes_big_dirs():
    root = tempfile.mkdtemp(prefix="bk_")
    try:
        os.makedirs(os.path.join(root, "src"))
        os.makedirs(os.path.join(root, "node_modules"))
        open(os.path.join(root, "src", "main.py"), "w").write("print(1)\n")
        open(os.path.join(root, "node_modules", "big.js"), "w").write("x" * 5000)
        r = bc.daily_backup(root, keep=3)
        assert r["ok"] is True
        assert r["snapshot"]["files"] == 1  # node_modules 被排除
        # 解压内容里没有 node_modules
        import zipfile
        with zipfile.ZipFile(r["snapshot"]["path"]) as zf:
            names = zf.namelist()
        assert not any("node_modules" in n for n in names)
    finally:
        _cleanup(root)


def test_backup_keep_limit():
    root = tempfile.mkdtemp(prefix="bk_")
    try:
        os.makedirs(os.path.join(root, "src"))
        open(os.path.join(root, "src", "a.py"), "w").write("x = 1\n")
        for i in range(5):
            # 模拟 5 天：用不同日期写快照（直接生成旧快照再跑 backup）
            date = f"2026081{i}"
            p = os.path.join(bc.BACKUP_ROOT, bc._slug(root), f"{date}.zip")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            bc._zip_dir(root, p)
        r = bc.daily_backup(root, keep=3)
        assert r["ok"] is True
        snaps = sorted(f for f in os.listdir(os.path.dirname(r["snapshot"]["path"]))
                       if f.endswith(".zip") and not f.startswith("."))
        assert len(snaps) <= 4  # 3 保留 + 今日新
        assert len(r["removed_old"]) >= 2  # 最旧的被删
    finally:
        _cleanup(root)


def test_rollback_restores_content():
    root = tempfile.mkdtemp(prefix="bk_")
    try:
        os.makedirs(os.path.join(root, "src"))
        open(os.path.join(root, "src", "main.py"), "w").write('print("hello")\n')
        r = bc.daily_backup(root, keep=3)
        date = r["date"]
        # 修改文件
        open(os.path.join(root, "src", "main.py"), "w").write('print("changed")\n')
        r2 = bc.rollback(root, date)
        assert r2["ok"] is True
        assert r2["pre_restore"]  # 当前状态已另存
        assert open(os.path.join(root, "src", "main.py")).read().strip() == 'print("hello")'
    finally:
        _cleanup(root)


def test_rollback_missing_snapshot():
    root = tempfile.mkdtemp(prefix="bk_")
    try:
        r = bc.rollback(root, "19990101")
        assert r["ok"] is False
        assert "快照不存在" in r["error"]
    finally:
        _cleanup(root)


def test_list_snapshots():
    root = tempfile.mkdtemp(prefix="bk_")
    try:
        os.makedirs(os.path.join(root, "src"))
        open(os.path.join(root, "src", "a.py"), "w").write("x = 1\n")
        bc.daily_backup(root, keep=3)
        r = bc.list_snapshots(root)
        assert r["ok"] is True
        assert r["count"] >= 1
        assert r["snapshots"][0]["snapshot"]
    finally:
        _cleanup(root)


def test_rollback_zip_slip_blocked():
    """恶意 zip（../ 成员）不得越界写文件（vuln-scan 2026-08-17 抓出并修复）。"""
    import zipfile
    root = tempfile.mkdtemp(prefix="zipslip_")
    try:
        evil = os.path.join(root, "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../evil_pwned.txt", "pwned")
            zf.writestr("ok.txt", "ok")
        bdir = os.path.join(bc.BACKUP_ROOT, bc._slug(root))
        os.makedirs(bdir, exist_ok=True)
        shutil.copy(evil, os.path.join(bdir, "20260101.zip"))
        r = bc.rollback(root, "20260101")
        assert r["ok"] is True
        assert not os.path.exists(os.path.join(os.path.dirname(root), "evil_pwned.txt"))
        assert os.path.exists(os.path.join(root, "ok.txt"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for p in glob.glob(os.path.join(bc.BACKUP_ROOT, "zipslip_*")):
            shutil.rmtree(p, ignore_errors=True)
