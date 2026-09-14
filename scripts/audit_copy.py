# -*- coding: utf-8 -*-
"""Mimosa 复审副本生成器（S138）：工作树 → 隔离副本（纪律：深扫只跑副本）。

用法：python scripts/audit_copy.py <dest>
- 拒绝 dest 在仓库内（防污染 git 工作树）；
- 跳过 .git/target/__pycache__/node_modules/.pytest_cache 与 .pyc；
- 输出：副本路径 + 文件数 + 源 HEAD 短 hash（台账记账用）。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {".git", "target", "__pycache__", "node_modules", ".pytest_cache"}


def main(argv):
    if len(argv) != 1:
        sys.exit("用法: python scripts/audit_copy.py <dest>")
    dest = os.path.abspath(argv[0])
    if dest == ROOT or dest.startswith(ROOT + os.sep):
        sys.exit(f"拒绝：dest 在仓库内（{dest}）——副本必须落在仓库外")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    n = 0
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, ROOT)
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    try:
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        head_s = (head.stdout or "").strip() or "?"
    except Exception:
        head_s = "?"
    print(f"AUDIT-COPY OK dest={dest} files={n} head={head_s}")


if __name__ == "__main__":
    main(sys.argv[1:])
