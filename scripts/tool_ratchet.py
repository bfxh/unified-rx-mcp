#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""tool_ratchet — 工具清单棘轮基线（RX 理念：约束进工具，repolint 棘轮模式）。

--check   校验 server._TOOLS/_EXT_DEFS 与 tools.json 基线一致，漂移即 exit 1
--update  重新生成 tools.json（仅限"有意变更工具清单"时使用，PR 必须解释）

基线三处声明（README/reasonix-plugin.json/冒烟测试）都锚定同一事实源：
这里生成的 tools.json。任何工具增删都会让 --check 红，防止"修过又回来"。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools.json"
sys.path.insert(0, str(ROOT))

_EXT_PREFIXES = ("cae_", "pr_oracle_", "tautest_")


def snapshot() -> dict:
    """当前真实工具清单（核心 _TOOLS + 懒加载扩展 _EXT_DEFS）。"""
    import server

    # 扩展是懒加载：--check 环境（非事件循环）用同步包装显式构建
    if not server._EXT_DEFS:
        server._ext_definitions()
    core = sorted(server._TOOLS.keys())
    ext = sorted(server._EXT_DEFS.keys())
    return {
        "core_count": len(core),
        "ext_count": len(ext),
        "total": len(core) + len(ext),
        "core": core,
        "ext": ext,
    }


def main() -> int:
    snap = snapshot()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    if mode == "--update":
        BASELINE.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[tool_ratchet] 基线已更新: {BASELINE} (total={snap['total']})")
        return 0

    if mode != "--check":
        print(f"[tool_ratchet] 未知模式: {mode}（--check / --update）", file=sys.stderr)
        return 2

    if not BASELINE.exists():
        print(
            f"[tool_ratchet] 基线缺失 {BASELINE}——先跑 --update 生成",
            file=sys.stderr,
        )
        return 2

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    diffs = []
    for key in ("core_count", "ext_count", "total", "core", "ext"):
        if base.get(key) != snap[key]:
            diffs.append(f"  {key}: 基线={base.get(key)!r} 实际={snap[key]!r}")

    if diffs:
        print("[tool_ratchet] FAIL 工具清单漂移（修 bug 时改了 _TOOLS？改完用 --update 更新基线并在 PR 说明）：")
        for d in diffs:
            print(d)
        return 1

    print(f"[tool_ratchet] OK 工具清单与基线一致 (core={snap['core_count']} ext={snap['ext_count']} total={snap['total']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
