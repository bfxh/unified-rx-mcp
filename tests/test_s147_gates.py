# -*- coding: utf-8 -*-
"""S147 契约：审核再上强度——历史明文门 / 依赖红线门 / 审计时效账本门。

三锁（都含"真门"负例，防止出现永远绿的摆设）：
1. secrets_history：块过滤口径（tests/ 豁免、产品面保留）+ 真跑绿（自给自足/仓内 dump）；
2. deps_lock：解析器对"有依赖"文本必须报红（纯函数负例）+ 真仓绿；
3. audit_ledger：真跑绿；压阈值（MAX_DAYS=0）必红；`--allow-stale` 显式放行；
   账本条数与 HARDENING 表行数对账（表/账本不许各说各话）。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import audit_ledger  # noqa: E402
import deps_lock  # noqa: E402
import secrets_history  # noqa: E402


def _py(script, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run([sys.executable, "-X", "utf8",
                           os.path.join(ROOT, "scripts", script), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, shell=False,
                          timeout=600)


def test_secrets_history_block_filter():
    text = ("@@COMMIT abc123 msg\n"
            "diff --git a/tools/x.py b/tools/x.py\n"
            "+++ b/tools/x.py\n"
            "+KEY = 'x'\n"
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "+++ b/tests/test_x.py\n"
            "+FAKE = 'y'\n")
    kept, blocks, commits = secrets_history.keep_blocks(text)
    assert commits == 1 and blocks == 1
    assert "tools/x.py" in kept and "tests/test_x.py" not in kept


def test_secrets_history_runs_green():
    cp = _py("secrets_history.py", "--n", "30")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "SECRETS-HISTORY OK" in cp.stdout


def test_deps_lock_detects_foreign_dep():
    bad = ('[package]\nname = "x"\n\n[dependencies]\n'
           'serde = "1"\n')
    assert deps_lock.offenders(bad), "有第三方依赖必须报红"
    good = '[dependencies]\n# 恒空\n'
    assert deps_lock.offenders(good) == []


def test_deps_lock_runs_green_on_repo():
    cp = _py("deps_lock.py")
    assert cp.returncode == 0 and "DEPS-LOCK OK" in cp.stdout


def test_audit_ledger_green_and_consistent():
    cp = _py("audit_ledger.py")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "AUDIT-LEDGER OK" in cp.stdout
    led = json.loads(open(os.path.join(ROOT, "spec", "audit-ledger.json"),
                          encoding="utf-8").read())
    hard = open(os.path.join(ROOT, "spec", "HARDENING.md"),
                encoding="utf-8").read()
    rows = [ln for ln in hard.splitlines() if ln.startswith("|") and "sha256:" in ln]
    assert len(rows) == len(led["entries"]), "表/账本条数不符"


def test_audit_ledger_stale_is_a_real_gate(tmp_path):
    """金丝雀：时效判据是真门。**自带临时账本**，不看仓里账本的日期/提交数——
    实测教训（S169）：仓里一旦"今天刚记入审计"，`MAX_DAYS=0` 就恒不触发（age=0 不 > 0 天），
    这条金丝雀会假绿（当时的"通过"只是因为上一次审计已经 10 天前）。"""
    led = tmp_path / "audit-ledger.json"
    led.write_text(json.dumps({"policy": "金丝雀", "entries": [
        {"round": "旧", "date": "2020-01-01", "copy": "t", "seal": "sha256:deadbeef",
         "findings": 1, "verdict": "inconclusive", "head": ""}]}), encoding="utf-8")
    tbl = tmp_path / "HARDENING.md"
    tbl.write_text("| 轮次 | 副本 | 封印 | 条数 | 运行状态 | 差量 |\n|---|---|---|---|---|---|\n"
                   "| 旧 | t | sha256:deadbeef… | 1 | inconclusive | — |\n", encoding="utf-8")
    env = {"UNIFIED_RX_AUDIT_LEDGER": str(led), "UNIFIED_RX_AUDIT_TABLE": str(tbl),
           "UNIFIED_RX_AUDIT_MAX_DAYS": "0"}
    cp = _py("audit_ledger.py", env_extra=env)
    assert cp.returncode != 0 and "STALE" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    cp2 = _py("audit_ledger.py", "--allow-stale", env_extra=env)
    assert cp2.returncode == 0 and "WARN-STALE" in cp2.stdout, cp2.stdout + cp2.stderr
