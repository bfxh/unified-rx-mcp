"""S147 契约：审核再上强度——历史明文门 / 依赖红线门 / 审计时效账本门。

三锁（都含"真门"负例，防止出现永远绿的摆设）：
1. secrets_history：块过滤口径（tests/ 豁免、产品面保留）+ 真跑绿（自给自足/仓内 dump）；
2. deps_lock：解析器对"有依赖"文本必须报红（纯函数负例）+ 真仓绿；
3. audit_ledger：真跑绿；压阈值（MAX_DAYS=0）必红；`--allow-stale` 显式放行；
   账本条数与 HARDENING 表行数对账（表/账本不许各说各话）。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

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


def test_timing_steps_skipped_unless_explicitly_enabled():
    """金丝雀：计时档（cli-bench / perf-gate）在**非独占**机器上默认跳过——但必须显式可见、且可显式开启。

    实测依据（2026-09-24，三次）：钩子档前几步刚把机器压满，随后测计时必红：cli-bench 报 `sys_devices`
    相对整批 1.64×，而**同一份输出里它自己算出的机器因子是 0.81×**（= 全机变慢，不是被测命令变慢），
    重跑即绿。故计时档默认 SKIP（显式、且总结行列出），要跑就设 `UNIFIED_RX_TIMING_GATES=1`。
    """
    target = "cli-golden,cli-bench,perf-gate"
    cp = _py("local_gate.py", "--fast", "--only", target)
    out = cp.stdout + cp.stderr
    assert "OK   cli-golden" in out, out                      # 正确性那半照跑（纯比对，与负载无关）
    assert "SKIP cli-bench" in out and "SKIP perf-gate" in out, out
    assert "skipped=['cli-bench', 'perf-gate']" in out, out    # 总结行必须列出被跳过的（不静默）
    assert cp.returncode == 0, out
    cp2 = _py("local_gate.py", "--fast", "--only", target,
              env_extra={"UNIFIED_RX_TIMING_GATES": "1"})
    out2 = cp2.stdout + cp2.stderr
    assert "SKIP cli-bench" not in out2 and "SKIP perf-gate" not in out2, out2


def test_dupe_gate_catches_new_duplicates(tmp_path):
    """金丝雀：重复代码门必须**真的会红**——造一对 100% 相同的文件，门要判"新增重复对"。

    与 god-gate 同纪律：门不做金丝雀就等于装饰。这里同时锁住"先记基线（单文件无对）→ 再造重复 ⇒ 红"。
    """
    body = "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(40))
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    cp0 = _py("dupe_gate.py", "--root", str(tmp_path), "--write-baseline")
    assert cp0.returncode == 0, cp0.stdout + cp0.stderr
    (tmp_path / "b.py").write_text(body, encoding="utf-8")          # 造重复（同内容）
    cp = _py("dupe_gate.py", "--root", str(tmp_path))
    assert cp.returncode == 1, "重复文件没判红——假门\n" + cp.stdout
    assert "新增重复对" in cp.stdout, cp.stdout


def test_type_gate_catches_new_type_error(tmp_path):
    """金丝雀（S171）：类型门零容忍——**注解过的**代码里出类型错必须判红。

    判据的坑：mypy 默认档**不查无注解函数体**（把 `return "x"` 放进无注解函数不会报），
    金丝雀若写成无注解版就是假绿。故此处用 `-> int` + `return "x"`；
    修好后还必须回绿（否则门是"恒红 = 摆设"）。
    """
    if importlib.util.find_spec("mypy") is None:
        pytest.skip("本机没有 mypy：门会 FAIL（不静默），但这条金丝雀无法验证")
    bad = tmp_path / "bad.py"
    bad.write_text('def f() -> int:\n    return "x"\n', encoding="utf-8")
    cp = _py("type_gate.py", "--root", str(tmp_path), "--paths", "bad.py")
    assert cp.returncode == 1, "注解代码里的类型错没判红——假门\n" + cp.stdout
    assert "return-value" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    bad.write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    cp2 = _py("type_gate.py", "--root", str(tmp_path), "--paths", "bad.py")
    assert cp2.returncode == 0, cp2.stdout + cp2.stderr


def test_lint_gate_catches_new_rule_hits(tmp_path):
    """金丝雀（S171）：Python 静态门是**逐规则棘轮**——涨一条就红，且认得出是哪条规则。

    自带临时根 + 自写 ruff.toml（不受仓内/机器级配置影响）；先记基线（记着那条 F821），
    再加一处同样的违规 ⇒ 必须 exit 1 且打印规则号。**顺序反了会假绿**，所以锁进测试。
    """
    if not (shutil.which("ruff") or shutil.which("ruff.exe")):
        pytest.skip("本机没有 ruff：门会 FAIL（不静默），但这条金丝雀无法验证")
    (tmp_path / "ruff.toml").write_text(
        'target-version = "py310"\n[lint]\nselect = ["F"]\n', encoding="utf-8")
    (tmp_path / "a.py").write_text("def f():\n    return undefined_name\n",
                                   encoding="utf-8")
    cp0 = _py("lint_gate.py", "--root", str(tmp_path), "--write-baseline")
    assert cp0.returncode == 0, cp0.stdout + cp0.stderr
    (tmp_path / "b.py").write_text("def g():\n    return other_missing\n",
                                   encoding="utf-8")
    cp = _py("lint_gate.py", "--root", str(tmp_path))
    assert cp.returncode == 1, "新增 F821 没判红——静态门是假门\n" + cp.stdout
    assert "F821" in cp.stdout, cp.stdout


def test_god_gate_fn_hard_threshold_is_a_real_gate(tmp_path):
    """金丝雀（S170）：`max_fn_lines` 是**硬阈**，基线祖父化也不放行。

    背景：本轮之前 7 个长函数被基线记着（棘轮内永远不红）⇒ 门对它们形同摆设。
    全仓拆到阈值以内后才开闸；这条同时锁住"先记基线、再验红"的顺序（反了会假绿）。
    """
    cfg = {"max_file_lines": 800, "max_fn_lines": 120, "max_type_members": 24,
           "include": ["**/*.py"], "exclude": [], "baseline": "god-baseline.json",
           "fn_hard_threshold": True}
    (tmp_path / "god.gate.json").write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / "long.py").write_text(
        "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(130)) + "    return 0\n",
        encoding="utf-8")
    cp0 = _py("god_gate.py", "--root", str(tmp_path), "--write-baseline")
    assert cp0.returncode == 0, cp0.stdout + cp0.stderr     # 基线里就记着这个长函数
    cp = _py("god_gate.py", "--root", str(tmp_path))
    assert cp.returncode == 1, "130 行函数没判红——硬阈是假门\n" + cp.stdout
    assert "硬阈" in cp.stdout, cp.stdout


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
