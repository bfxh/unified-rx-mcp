# -*- coding: utf-8 -*-
"""S150 契约：命令行加速的证据链——金标准（质量不变）+ 计时（真的更快）。

用户指令：「命令行需要**不变质量的情况下加速**」。本文件把这句话变成机器约束：
1. 金标准在册：`spec/cli-golden.json` 覆盖 ≥8 条代表命令（每条 = 退出码 + stdout
   SHA256 + 字节数）；**天然不确定**的命令（如活进程列表）必须被排除在承诺外；
2. 金标准与计时**真跑绿**（`bench/cli_bench.py --check-golden --check`）；
3. **性能配置锁**：`[profile.release]` 的 LTO/单编译单元/去符号必须保留——
   改它必须同时改本测试并写明理由（"加速是承诺，不是一次性动作"）。
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench", "cli_bench.py")
GOLDEN = os.path.join(ROOT, "spec", "cli-golden.json")
BASE = os.path.join(ROOT, "spec", "cli-baseline.json")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_golden_and_baseline_are_in_place():
    g = json.loads(_read(GOLDEN))["commands"]
    b = json.loads(_read(BASE))["commands"]
    assert len(g) >= 8, f"金标准覆盖太少: {len(g)}"
    assert len(b) >= len(g), "计时基线不应少于金标准命令数"
    for k, v in g.items():
        assert set(v) == {"rc", "sha", "out_bytes"}, f"{k} 金标准字段不完整"
        assert len(v["sha"]) == 16
    # 天然不确定的命令不许进金标准（承诺必须是可复现的）
    assert "sys_procs" not in g, "活进程列表命令不得进入金标准承诺"


def test_cli_bench_runs_green():
    cp = subprocess.run([sys.executable, "-X", "utf8", BENCH,
                         "--check-golden", "--check"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=ROOT, shell=False, timeout=900)
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, out[-1500:]
    assert "CLI-GOLDEN OK" in cp.stdout, cp.stdout[-500:]
    assert "CLI-BENCH OK" in cp.stdout, cp.stdout[-500:]


def test_release_profile_keeps_speed_settings():
    """性能配置锁（S150 实测：−29% 启动时间；回退必须记账）。"""
    cargo = _read(os.path.join(ROOT, "rust", "Cargo.toml"))
    sec = cargo[cargo.index("[profile.release]"):]
    # 只看生效配置，忽略注释行（注释里可以讨论被否决的选项）
    sec = "\n".join(ln for ln in sec.splitlines()
                    if not ln.strip().startswith("#"))
    assert re.search(r'lto\s*=\s*"fat"', sec), "LTO 被移除（加速回退）"
    assert re.search(r"codegen-units\s*=\s*1", sec), "单编译单元被移除"
    assert re.search(r'strip\s*=\s*"symbols"', sec), "strip 被移除"
    assert not re.search(r'panic\s*=\s*"abort"', sec), \
        "panic=abort 无实测收益且改失败语义——不应引入"
