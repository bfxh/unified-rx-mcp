#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rx-core（Rust 纯函数层）接线验收测试。

- parity：Python vs Rust 输出一致性（2310 随机/边界用例）
- Rust 直调：_rxcore_call 真实走常驻子进程
- 回退：RX_CORE=0 时纯函数仍可用（Python 版）
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(server._RX_CORE_EXE is None,
                    reason="rx-core 未编译（target/release 缺失）")
def test_rust_dispatch():
    """Rust 常驻子进程真实执行（非 Python 回退）。"""
    assert server._rxcore_call("math_factorial", {"n": 10}) == "3628800"
    assert server._rxcore_call("fib", {"n": 20}) == "6765"
    assert server._rxcore_call("str_palindrome", {"s": "abccba"}) == "True"


@pytest.mark.skipif(server._RX_CORE_EXE is None,
                    reason="rx-core 未编译")
def test_parity_full():
    """Python vs Rust 全量一致性（一期验收标准：0 mismatches）。"""
    script = os.path.join(ROOT, "rx-core", "parity_check.py")
    r = subprocess.run([sys.executable, script], capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert "0 mismatches" in out, f"parity 不一致: {out[-400:]}"


def test_fallback_python():
    """RX_CORE=0 → 整体回退 Python，纯函数输出一致。"""
    os.environ["RX_CORE"] = "0"
    try:
        r = server._call("math_ops", {"action": "power", "base": 2, "exponent": 10})
        assert r[0].text == "1024"
        r2 = server._call("prime_list", {"action": "is_prime", "n": 97})
        assert r2[0].text == "true"
    finally:
        os.environ["RX_CORE"] = "1"


def test_tool_chain_rust():
    """工具链路（math_ops/prime_list 组合）在 Rust 启用时正常。"""
    if server._RX_CORE_EXE is None:
        pytest.skip("rx-core 未编译")
    assert server._call("math_ops", {"action": "factorial", "n": 5})[0].text == "120"
    assert server._call("math_ops", {"action": "power", "base": 2, "exponent": 10})[0].text == "1024"
    assert server._call("prime_list", {"action": "is_prime", "n": 97})[0].text == "true"
