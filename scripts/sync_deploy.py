#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""部署同步：仓库 -> E:/共享/51/unified-rx（运行副本）。

合并去重后（2026-08-16）部署目录是唯一运行副本（config.toml 只注册
unified-rx；cae/pr-oracle/tautest/stats/ci-optimization 均为仓库内
vendor 扩展，不再独立注册）。同步内容：
  1. 顶层 .py / .json（server.py + 模块 + tools.json + manifest）
  2. vendor/（扩展，含 ci-optimization 独立化 src）
  3. Rust exe（rx-core/rx-search/rx-telemetry/rx-net 的 target/release，
     中文路径仓库编译产物固定 D:/rj/.rx-target，需拷回部署目录）
用法：python scripts/sync_deploy.py [--dry-run]
"""
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join("E:", os.sep, "共享", "51", "unified-rx")
DRY = "--dry-run" in sys.argv

_RUST_CRATES = ("rx-core", "rx-search", "rx-telemetry", "rx-net")


def main() -> int:
    if not os.path.isdir(DEPLOY):
        print(f"部署目录不存在: {DEPLOY}")
        return 1
    n = 0

    def sync(src: str, dst: str, is_dir: bool = False) -> None:
        nonlocal n
        if DRY:
            print(f"[dry] {src} -> {dst}")
            n += 1
            return
        if is_dir:
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        n += 1

    # 1) 顶层模块与清单（跳过测试/文档/构建产物目录）
    for name in sorted(os.listdir(REPO)):
        full = os.path.join(REPO, name)
        if name.startswith((".", "_")):
            continue
        if name in ("scripts", "docs", "spec", "probes", "reports",
                    "assets", "sec-workflows", "skill_templates", "skills",
                    "test_", "_probe_tmp") or name.startswith("test_"):
            continue
        if os.path.isfile(full) and (name.endswith(".py") or name.endswith(".json")):
            sync(full, os.path.join(DEPLOY, name))
    # 2) vendor 扩展
    sync(os.path.join(REPO, "vendor"), os.path.join(DEPLOY, "vendor"), is_dir=True)
    # 3) Rust exe（release）
    for crate in _RUST_CRATES:
        src = os.path.join(REPO, crate, "target", "release", f"{crate}.exe")
        if os.path.exists(src):
            dst = os.path.join(DEPLOY, crate, "target", "release", f"{crate}.exe")
            sync(src, dst)
    print(f"{'[dry-run] ' if DRY else ''}同步完成: {n} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
