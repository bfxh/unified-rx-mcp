#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""async_guard — 同步路径禁用 asyncio.run（REGRESSION_GUARD P1-3）。

背景：MCP async handler 内 asyncio.run() 抛 "cannot be called from a running
event loop"（2026-08-11 真实崩溃）。本脚本静态扫描 server.py 的同步函数
（_definitions/_call/_call_ext/_tool_* 等，非 async def），断言其函数体内
不直接出现 asyncio.run( 调用——防止修复回归。

用法：python scripts/async_guard.py <server.py>  （exit 0 = 通过）
"""

import re
import sys
from pathlib import Path


# 白名单：命令行自检（__main__ 顶层，非协议层）与同步包装
# （_ext_definitions 注释明确"仅测试/无事件循环环境"）——合法 asyncio.run
_ALLOWED_FNS = {"_selftest", "_ext_definitions"}


def scan(path: Path) -> list[str]:
    """返回违规列表：[(行号, 函数名)]。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    violations: list[str] = []

    # 函数边界粗定位：行 → 所属函数（最近的上方 def/async def）
    fn_stack: list[str] = []
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(?:async\s+)?def\s+(\w+)\s*\(", ln)
        if m:
            fn_stack.append(m.group(1))
        if "asyncio.run(" in ln and "def " not in ln:
            if fn_stack and fn_stack[-1] not in _ALLOWED_FNS:
                # 同步函数内禁止 asyncio.run；async def 内同样禁止（嵌套循环）
                violations.append(f"{path.name}:{i} 函数 {fn_stack[-1]}: 含 asyncio.run(")
            elif not fn_stack:
                violations.append(f"{path.name}:{i}: 模块级 asyncio.run(")
        # 粗略模拟函数结束（下一 def 前）——用栈简化：记录最近 def 即够
    return violations


def main() -> int:
    targets = sys.argv[1:] or [str(Path(__file__).resolve().parent.parent / "server.py")]
    bad = 0
    for t in targets:
        p = Path(t)
        if not p.exists():
            print(f"[async_guard] 文件不存在: {t}", file=sys.stderr)
            bad += 1
            continue
        v = scan(p)
        if v:
            bad += 1
            print(f"[async_guard] FAIL {p.name}:")
            for item in v:
                print("  ", item)
        else:
            print(f"[async_guard] OK {p.name}: 同步路径无 asyncio.run")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
