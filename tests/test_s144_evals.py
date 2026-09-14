# -*- coding: utf-8 -*-
"""S144 契约：任务级工具评测（EXTERNAL-ALIGNMENT B2）。

三锁：
1. 评测真跑绿——`bench/tool_evals.py --check`（剥掉沙盒 env 自给自足，同 secrets
   门纪律）；13 个确定性任务全过 + 体量不超基线 ×1.10；
2. 基线在册——`spec/tool-evals-baseline.json` 任务数 ≥13、每题有 ok/calls/chars；
3. 真门验证——`UNIFIED_RX_EVAL_SABOTAGE=1` 时任务 1 必红（防止出现"永远绿"的
   假门：评测脚本自身也要被证明能判红）。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bench", "tool_evals.py")
BASELINE = os.path.join(ROOT, "spec", "tool-evals-baseline.json")


def _run(extra_env=None, args=("--check",)):
    env = {k: v for k, v in os.environ.items() if k != "UNIFIED_RX_SANDBOX"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, shell=False,
                          timeout=600)


def test_tool_evals_green():
    cp = _run()
    assert cp.returncode == 0, f"评测红:\n{cp.stdout}\n{cp.stderr}"
    assert "TOOL-EVALS OK" in cp.stdout
    assert "failed=[]" not in cp.stdout          # OK 路径不出现 failed 清单
    assert "TOTAL tasks=13" in cp.stdout


def test_baseline_in_registry():
    with open(BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    tasks = base.get("tasks") or {}
    assert len(tasks) >= 13, f"基线任务数不足: {len(tasks)}"
    assert "note" in base
    for name, ent in tasks.items():
        assert set(("ok", "calls", "chars")) <= set(ent), name


def test_tool_evals_is_a_real_gate():
    cp = _run({"UNIFIED_RX_EVAL_SABOTAGE": "1"})
    assert cp.returncode != 0, "sabotage 模式还绿——这是假门"
    assert "TOOL-EVALS FAIL" in (cp.stdout + cp.stderr)
