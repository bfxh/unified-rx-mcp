# -*- coding: utf-8 -*-
"""本地审核门（S145，用户指令：「把审核这个东西搞强点，不需要用 GitHub 和 Linux
就直接搞这个流程」）。

一条命令跑完**与 CI 完全同一套**审核（脚本级同源，不漂移由测试锁死）：
  快门（--fast，秒级）：secrets 明文红线 / self-attack 自攻 / data-flow 数据流 /
    toolface 工具面体量 / tool-evals 任务级评测 / selftest 对账硬门
  全门（缺省）：快门 + pytest 全量 + cargo test + clippy 零告警

用法（Windows/Git Bash/Linux 通用，纯 stdlib）：
  python -X utf8 scripts/local_gate.py            # 全门（合入前跑）
  python -X utf8 scripts/local_gate.py --fast     # 快门（pre-commit 用）
  python -X utf8 scripts/local_gate.py --list     # 列出步骤
  python -X utf8 scripts/local_gate.py --only secrets,toolface
  python -X utf8 scripts/local_gate.py --no-cargo # 无 Rust 工具链时（不静默：显式）
  UNIFIED_RX_GATE_FORCE_FAIL=secrets …            # 自检：注入指定步骤失败（真门验证）

纪律：cargo 缺失默认 **FAIL 不静默**（红线是"pytest + cargo 双绿"，缺一不可；
确需跳过请显式 --no-cargo）；每步输出失败尾部便于就地修复。
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
CARGO = shutil.which("cargo") or "cargo"

# (名字, argv, 档位, 说明)——与 .github/workflows/core.yml 的硬门同源（drift 由
# tests/test_s145_gates.py 锁：core.yml 里出现的门脚本必须都在这里）。
STEPS = [
    ("secrets",     [PY, "-X", "utf8", "scripts/ci_secrets_gate.py"], "fast", "明文红线（key 门）"),
    ("path-gate",   [PY, "-X", "utf8", "scripts/path_gate.py"], "fast", "路径门（符号链接/文件名卫生/越界写）"),
    ("self-attack", [PY, "-X", "utf8", "scripts/attack_gate.py"], "fast", "自攻门（巡航 clean）"),
    ("data-flow",   [PY, "-X", "utf8", "scripts/taint_gate.py"], "fast", "数据流门（taint 基线）"),
    ("secrets-history", [PY, "-X", "utf8", "scripts/secrets_history.py"], "fast", "历史 diff 明文红线"),
    ("deps-lock",   [PY, "-X", "utf8", "scripts/deps_lock.py"], "fast", "依赖红线（Cargo 恒空）"),
    ("audit-freshness", [PY, "-X", "utf8", "scripts/audit_ledger.py"], "fast", "审计时效/账本对账"),
    ("toolface",    [PY, "-X", "utf8", "scripts/toolface_budget.py"], "fast", "工具面体量软帽"),
    ("tool-evals",  [PY, "-X", "utf8", "bench/tool_evals.py", "--check"], "fast", "任务级评测基线"),
    ("cli-bench",   [PY, "-X", "utf8", "bench/cli_bench.py", "--check-golden",
                     "--check"], "fast", "命令行：输出金标准 + 计时（不变质量）"),
    ("perf-gate",   [PY, "-X", "utf8", "scripts/perf_gate.py"], "fast", "性能门：并行/串行比值"),
    ("mcp-surface", [PY, "-X", "utf8", "scripts/mcp_surface_gate.py"], "fast", "协议面门：真握手契约"),
    ("model-fit",   [PY, "-X", "utf8", "scripts/model_fit_gate.py"], "fast", "模型适配门（弱模型模拟 + 回包预算）"),
    ("selftest",    [PY, "-X", "utf8", "scripts/ci_gate.py"], "fast", "对账硬门（SCHEMA/EXE/VERSION）"),
    ("pytest",      [PY, "-m", "pytest", "tests/", "-q"], "full", "全量测试"),
    ("cargo-test",  [CARGO, "test", "--manifest-path", "rust/Cargo.toml"], "full", "Rust 测试"),
    ("clippy",      [CARGO, "clippy", "--manifest-path", "rust/Cargo.toml",
                     "--all-targets", "--", "-D", "warnings"], "full", "clippy 零告警"),
]


def _child_env(step_name):
    """子进程环境：PYTHONUTF8 全局；**沙盒只给 selftest 步**——pytest 自己由
    conftest 管理沙盒，全局注入会与测试夹具的隔离打架（S145 实锤：284 红）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if step_name == "selftest":
        env["UNIFIED_RX_SANDBOX"] = ROOT
    return env


def _run_step(name, argv, force_fail=None):
    if force_fail == name:
        return False, 0.0, f"[自检注入] UNIFIED_RX_GATE_FORCE_FAIL={name}"
    t0 = time.time()
    cp = subprocess.run(argv, cwd=ROOT, env=_child_env(name), shell=False,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=3600)
    out = (cp.stdout or "") + (("\n" + cp.stderr) if cp.stderr else "")
    return cp.returncode == 0, time.time() - t0, out


def main(argv):
    want_fast = "--fast" in argv
    no_cargo = "--no-cargo" in argv
    if "--list" in argv:
        for n, _a, tier, why in STEPS:
            print(f"{tier:4s} {n:12s} {why}")
        return 0
    only = None
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            only = {s.strip() for s in argv[i + 1].split(",") if s.strip()}
    force_fail = os.environ.get("UNIFIED_RX_GATE_FORCE_FAIL")
    rows, failed = [], []
    for name, cmd, tier, why in STEPS:
        if want_fast and tier != "fast":
            continue
        if only is not None and name not in only:
            continue
        if no_cargo and name in ("cargo-test", "clippy"):
            print(f"SKIP {name:12s} --no-cargo（显式跳过，红线语义：不算双绿）")
            continue
        if name in ("cargo-test", "clippy") and not shutil.which("cargo"):
            print(f"FAIL {name:12s} cargo 不可用——不静默降级（--no-cargo 显式跳过）")
            failed.append(name)
            continue
        ok, secs, out = _run_step(name, cmd, force_fail)
        rows.append((name, ok, secs, why, out))
        print(f"{'OK  ' if ok else 'FAIL'} {name:12s} {secs:6.1f}s {why}")
        if not ok:
            failed.append(name)
            # S159：失败步骤**完整输出落盘**（尾部打印常被外层管道截断——本轮实测：
            # 一次瞬时失败因 `| tail -3` 丢掉全部细节）。路径 resolve 后校验在根内。
            try:
                from pathlib import Path
                base = Path(os.environ.get("TEMP", ".")).resolve()
                fp = (base / f"unrx-gate-fail-{name}.log").resolve()
                if base in fp.parents:
                    fp.write_text(out or "", encoding="utf-8")
                    print(f"  （完整输出已落盘：{fp}）")
            except OSError:
                pass
            tail = out.strip().splitlines()[-12:]
            print("  " + "\n  ".join(tail))
    total = sum(r[2] for r in rows)
    print(f"LOCAL-GATE {'OK' if not failed else 'FAIL'} steps={len(rows)} "
          f"failed={failed} total={total:.1f}s")
    if failed:
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
