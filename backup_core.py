#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""backup_core.py — 每日备份 + 回溯（2026-08-17）。

用户要求（2026-08-17）："搞项目的时候会每天备份，备份不会太多" +
"增加回溯的效果"。

- daily_backup(root)：① git 仓库自动 commit + tag（daily-YYYYMMDD）
  ② 项目快照压缩到 ~/.unified-rx/backups/<slug>/<YYYYMMDD>.zip（限量 keep 份，
  默认 7——"备份不会太多"；排除 node_modules/.git/target 等大目录）
- list_snapshots(root)：备份时间线
- rollback(root, date)：回溯到指定快照（恢复前自动把当前状态另存
  .pre-restore-<ts>.zip——防误操作不可逆）
"""
import datetime
import json
import os
import shutil
import subprocess
import time
import zipfile

STATE_DIR = os.path.join(os.path.expanduser("~"), ".unified-rx")
BACKUP_ROOT = os.path.join(STATE_DIR, "backups")

# 快照排除目录（大/可再生内容——控制备份体积）
_EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build",
                 "target", "models", ".venv", "venv", "env", ".idea",
                 ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                 "lse-engine/target", ".unified-rx"}


def _slug(root: str) -> str:
    return os.path.basename(os.path.normpath(root)).replace(" ", "_") or "project"


def _date_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _git(root: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _zip_dir(root: str, zip_path: str) -> int:
    """压缩项目目录（排除大目录），返回文件数。

    zip_slip 说明（vuln-scan 2026-08-17）：zip 成员名由 os.path.relpath
    生成（相对 root，无用户输入、无 ../）——**写入方向**无越界可能；
    解压方向（rollback）已做 startswith(root_abs) 防护。
    """
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > 100_000_000:  # 单文件 >100MB 跳过
                        continue
                    zf.write(fp, os.path.relpath(fp, root))
                    count += 1
                except OSError:
                    continue
    return count


def daily_backup(root: str, keep: int = 7, do_git: bool = True) -> dict:
    """每日备份：git commit + tag + 限量快照 zip。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    if not 1 <= keep <= 30:
        return {"ok": False, "error": "keep 须在 1..30"}
    root = os.path.normpath(root)
    slug = _slug(root)
    date = _date_str()
    report: dict = {"ok": True, "root": root, "date": date, "slug": slug}

    # 1) git 自动提交 + tag
    git_ok = False
    if do_git and os.path.isdir(os.path.join(root, ".git")):
        _git(root, "add", "-A")
        diff = _git(root, "diff", "--cached", "--stat")
        if diff:
            _git(root, "commit", "-q", "-m", f"daily backup {date}")
        _git(root, "tag", f"daily-{date}", force=True)
        git_ok = True
    report["git"] = {"ok": git_ok, "tag": f"daily-{date}",
                     "note": "非 git 仓库跳过 git 提交" if not git_ok else "已提交+打 tag"}

    # 2) 快照 zip（限量 keep 份）
    proj_dir = os.path.join(BACKUP_ROOT, slug)
    os.makedirs(proj_dir, exist_ok=True)
    zip_path = os.path.join(proj_dir, f"{date}.zip")
    if os.path.exists(zip_path):
        report["snapshot"] = {"path": zip_path, "skipped": "今日已备份"}
    else:
        files = _zip_dir(root, zip_path)
        report["snapshot"] = {"path": zip_path, "files": files,
                              "size_mb": round(os.path.getsize(zip_path) / 1_048_576, 1)}

    # 3) 限量清理（删最旧，保留 keep 份）
    snaps = sorted(f for f in os.listdir(proj_dir) if f.endswith(".zip"))
    removed = []
    while len(snaps) > keep:
        old = snaps.pop(0)
        try:
            os.remove(os.path.join(proj_dir, old))
            removed.append(old)
        except OSError:
            continue
    report["snapshots"] = snaps
    report["removed_old"] = removed
    report["keep"] = keep
    report["backup_root"] = proj_dir
    return report


def list_snapshots(root: str) -> dict:
    """备份时间线（该项目的快照列表 + 大小）。"""
    slug = _slug(root)
    proj_dir = os.path.join(BACKUP_ROOT, slug)
    snaps = []
    if os.path.isdir(proj_dir):
        for fn in sorted(f for f in os.listdir(proj_dir) if f.endswith(".zip")):
            p = os.path.join(proj_dir, fn)
            try:
                st = os.stat(p)
                snaps.append({"snapshot": fn[:-4], "path": p,
                              "size_mb": round(st.st_size / 1_048_576, 1),
                              "ts": st.st_mtime})
            except OSError:
                continue
    return {"ok": True, "root": root, "slug": slug, "snapshots": snaps,
            "count": len(snaps), "backup_root": proj_dir}


def rollback(root: str, date: str) -> dict:
    """回溯到指定日期快照（恢复前先把当前状态另存 .pre-restore-<ts>.zip）。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    root = os.path.normpath(root)
    slug = _slug(root)
    zip_path = os.path.join(BACKUP_ROOT, slug, f"{date}.zip")
    if not os.path.isfile(zip_path):
        return {"ok": False, "error": f"快照不存在: {date}（list_snapshots 查看可用日期）"}
    # 1) 当前状态另存（可逆）
    pre = os.path.join(BACKUP_ROOT, slug, f".pre-restore-{int(time.time())}.zip")
    try:
        _zip_dir(root, pre)
    except Exception as e:
        return {"ok": False, "error": f"当前状态另存失败（中止恢复）: {e}"}
    # 2) 解压覆盖（先删目标内容再解压——排除大目录由 zip 内容决定）
    # zip_slip 防护（vuln-scan 2026-08-17 抓出）：成员路径必须落在 root 内，
    # 否则跳过（恶意 zip 的 ../ 成员会越界写文件）
    root_abs = os.path.abspath(root) + os.sep
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(root, member))
                if not target.startswith(root_abs):
                    continue  # 越界成员跳过（zip_slip 防护）
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except Exception as e:
        return {"ok": False, "error": f"恢复失败: {e}", "pre_restore": pre}
    return {"ok": True, "restored": date, "root": root,
            "pre_restore": pre,
            "note": "当前状态已另存 .pre-restore zip——如需撤销恢复可手动还原"}


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 1:
        root = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        if sys.argv[1] == "backup":
            print(json.dumps(daily_backup(root), ensure_ascii=False, indent=2))
        elif sys.argv[1] == "list":
            print(json.dumps(list_snapshots(root), ensure_ascii=False, indent=2))
        elif sys.argv[1] == "rollback":
            print(json.dumps(rollback(root, sys.argv[3]), ensure_ascii=False, indent=2))
    else:
        print("用法: backup <root> | list <root> | rollback <root> <YYYYMMDD>")
