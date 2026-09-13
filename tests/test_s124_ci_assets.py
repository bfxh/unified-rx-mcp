# -*- coding: utf-8 -*-
"""S124 契约：CI 工作流程资产不许悄悄变弱（严苛纪律的机器化）。

形状锁：pytest 双解释器矩阵 / cargo build+test+clippy / 秘密硬门禁 /
selftest 硬门禁 / fetch-depth: 0 / 周扫 workflow 带 schedule。
谁删步骤谁红——门禁只许加强不许悄悄退役。
"""
import os
import py_compile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, ".github", "workflows", "core.yml")
SCAN = os.path.join(ROOT, ".github", "workflows", "scan.yml")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_core_workflow_keeps_hard_gates():
    src = _read(CORE)
    for needle in ("'3.11'", "'3.14'",             # 双解释器矩阵（缺一个=降级）
                   "cargo build --release",         # exe 必建（不做静默降级）
                   "cargo test",                    # 双绿纪律的 Rust 侧
                   "clippy",                        # 零告警红线
                   "scripts/ci_secrets_gate.py",    # 明文红线机器化
                   "scripts/ci_gate.py",            # selftest 硬门禁
                   "fetch-depth: 0",                # VERSION_TAG 真对账
                   # S125：本机 config.toml 的 target-dir 是绝对路径，CI 必须 env 覆盖，
                   # 否则 exe 落错位置 → EXE_TAG 硬门禁失败（S124 首跑 CI 实锤）
                   "CARGO_TARGET_DIR",
                   "UNIFIED_RX_SANDBOX"):           # selftest 内部 fs 自检在沙盒内跑
        assert needle in src, f"core.yml 丢了 {needle!r}——门禁只许加强不许退役"


def test_weekly_scan_workflow_exists_with_schedule():
    src = _read(SCAN)
    assert "schedule:" in src and "cron:" in src, "周扫 workflow 缺 schedule"
    assert "scripts/ci_secrets_gate.py" in src, "周扫没挂秘密门禁"


def test_gate_scripts_compile():
    for name in ("ci_gate.py", "ci_secrets_gate.py"):
        p = os.path.join(ROOT, "scripts", name)
        assert os.path.isfile(p), f"缺 {name}"
        py_compile.compile(p, doraise=True)
