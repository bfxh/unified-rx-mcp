#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""契约探针套件（probes/run_all.py）的 pytest 包装——CI/交付验证入口。

run_all.py 退出码 0 = 全部探针通过（含 p07 net_chaos / p08 rx-core）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def test_all_probes_pass():
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "probes", "run_all.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"探针失败:\n{out[-1500:]}"
    assert "passed" in out, f"输出异常:\n{out[-500:]}"
