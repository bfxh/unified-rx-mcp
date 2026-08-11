#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""sync_check — dev 仓库 vs 生产副本一致性校验（REGRESSION_GUARD P2-1）。

对比 dev（unified-rx 仓库）与生产副本 E:\\共享\\51\\unified-rx 的核心文件，
不一致即 exit 1（双副本漂移是 bug 温床）。

用法：python scripts/sync_check.py            # 默认对比 51 副本
      python scripts/sync_check.py <prod_dir> # 指定副本目录
"""

import os
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
DEFAULT_PROD = Path(os.environ.get("UNIFIED_RX_PROD", r"E:\共享\51\unified-rx"))

# 核心同步清单（与 51 副本保持一致；扩展/脚本/CI 不在运行时同步范围）
_FILES = [
    "server.py",
    "test_unified_rx.py",
    "lse_client.py",
    "reasonix-plugin.json",
    "std_core.py",
    "cb_index_core.py",
    "ds_core.py",
    "locate_core.py",
    "ui_check_core.py",
    "BUG_SCAN_DESIGN.md",
    "README.md",
    "lse-engine/src/lib.rs",
    "lse-engine/src/main.rs",
    "lse-engine/Cargo.toml",
]


def main() -> int:
    prod = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PROD
    bad = 0
    for rel in _FILES:
        dev_f = DEV / rel
        prod_f = prod / rel
        if not dev_f.exists():
            print(f"[sync_check] dev 缺失（清单过期?）: {rel}")
            continue
        if not prod_f.exists():
            print(f"[sync_check] DIFF {rel}: 生产副本缺失")
            bad += 1
            continue
        if dev_f.read_bytes() != prod_f.read_bytes():
            print(f"[sync_check] DIFF {rel}: 内容不一致（cp 同步）")
            bad += 1
    if bad:
        print(f"[sync_check] FAIL {bad} 个文件不同步（副本: {prod}）")
        return 1
    print(f"[sync_check] OK {len(_FILES)} 个核心文件与副本一致（{prod}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
