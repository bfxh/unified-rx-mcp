"""probe_08：rx-core（Rust 纯函数层）接线契约。

验证：
  p08a Rust 常驻子进程可用（_rxcore_call 直达）
  p08b Python vs Rust parity 一致性（跑 rx-core/parity_check.py，0 mismatch）
  p08c RX_CORE=0 禁用 → 回退 Python 不炸（纯函数仍工作）
"""
import json
import os
import subprocess
import sys

from _common import probe, REPO_ROOT
import server as S


@probe("p08a_rust_dispatch")
def p08a():
    """rx-core exe 就绪 + Rust 子进程真实执行。"""
    if S._RX_CORE_EXE is None:
        return False, "rx-core exe 未编译（target/release 缺失）"
    r = S._rxcore_call("math_factorial", {"n": 10})
    if r == "3628800":
        return True, f"Rust 直调成功: factorial(10)={r} (exe={os.path.basename(S._RX_CORE_EXE)})"
    return False, f"Rust 直调异常: {r!r}"


@probe("p08b_parity")
def p08b():
    """Python vs Rust 输出一致性（一期验收标准）。"""
    script = os.path.join(REPO_ROOT, "rx-core", "parity_check.py")
    r = subprocess.run([sys.executable, script], capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    if "0 mismatches" in out:
        return True, out.strip().splitlines()[-1]
    return False, f"parity 不一致: {out[-300:]}"


@probe("p08c_fallback")
def p08c():
    """RX_CORE=0 → 整体回退 Python，纯函数仍可用。"""
    os.environ["RX_CORE"] = "0"
    try:
        out = S._call("math_ops", {"action": "power", "base": 2, "exponent": 10})
        txt = out[0].text
        ok = txt == "1024"
        return (True, "RX_CORE=0 回退 Python 正常: 2^10=1024") if ok else \
               (False, f"回退异常: {txt!r}")
    finally:
        os.environ["RX_CORE"] = "1"
