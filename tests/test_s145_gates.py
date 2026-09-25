"""S145 契约：本地审核门（不依赖 GitHub/Linux）+ 协议协商与握手留痕。

四锁：
1. **本地门与 CI 同源不漂移**——core.yml 里出现的每道门脚本都必须在本仓
   `scripts/local_gate.py` 的步骤里（防"CI 加门、本地没加"或反向漂移）；
2. **钩子分档**——pre-commit 跑快门（--fast）、pre-push 跑全门（含双测/clippy）；
3. **真门验证**——UNIFIED_RX_GATE_FORCE_FAIL 注入必须让门判红（不是永远绿）；
4. **握手审计（B1）**——版本协商白名单语义 + 留痕落盘（谁/什么版本/协商结果）
   + 留痕失败永不阻断握手。
"""
import json
import os
import re
import subprocess
import sys

import server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "local_gate.py")
CORE = os.path.join(ROOT, ".github", "workflows", "core.yml")
STEPS = ("secrets", "path-gate", "self-attack", "data-flow", "secrets-history", "deps-lock",
         "audit-freshness", "toolface", "tool-evals", "cli-bench", "perf-gate",
         "mcp-surface", "model-fit", "selftest", "stress", "pytest", "cargo-test", "clippy",
         "god-gate", "dupe-gate", "stdout-gate", "nest-gate", "args-gate", "gate-probe")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _py(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-X", "utf8", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, shell=False, timeout=600)


def test_local_gate_covers_every_ci_gate_script():
    core = _read(CORE)
    gate = _read(GATE)
    ci_scripts = set(re.findall(r"(?:scripts/[a-z0-9_]+\.py|bench/tool_evals\.py)", core))
    missing = {s for s in ci_scripts if s not in gate}
    assert not missing, f"CI 有而本地门没有（漂移）: {sorted(missing)}"


def test_hooks_split_fast_and_full():
    pre_commit = _read(os.path.join(ROOT, ".githooks", "pre-commit"))
    pre_push = _read(os.path.join(ROOT, ".githooks", "pre-push"))
    assert "local_gate.py --fast" in pre_commit, "pre-commit 必须只跑快门"
    assert "local_gate.py" in pre_push and "--fast" not in pre_push, \
        "pre-push 必须跑全门（双绿红线）"


def test_local_gate_force_fail_is_a_real_gate():
    env = dict(os.environ, UNIFIED_RX_GATE_FORCE_FAIL="toolface")
    cp = subprocess.run([sys.executable, "-X", "utf8", GATE, "--only", "toolface"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, cwd=ROOT, shell=False, timeout=300)
    assert cp.returncode != 0, "注入失败还绿——假门"
    assert "LOCAL-GATE FAIL" in cp.stdout


def test_local_gate_list_shows_all_steps():
    cp = _py(GATE, "--list")
    assert cp.returncode == 0
    for n in STEPS:
        assert n in cp.stdout, f"步骤 {n} 不在 --list"


def test_negotiate_version_semantics():
    assert server._negotiate_version("2025-03-26") == "2025-03-26"
    assert server._negotiate_version("2026-07-28") == server.PROTOCOL_VERSION
    assert server._negotiate_version(None) == server.PROTOCOL_VERSION
    assert server._negotiate_version(20250326) == server.PROTOCOL_VERSION


def test_handshake_recorded_and_reply_negotiated(tmp_path):
    p = tmp_path / "clients.jsonl"
    os.environ["UNIFIED_RX_CLIENTS_LOG"] = str(p)
    try:
        r = server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2026-07-28",
                                       "clientInfo": {"name": "zcode",
                                                      "version": "9.9"}}})
        assert r["result"]["protocolVersion"] == server.PROTOCOL_VERSION
        line = json.loads(p.read_text(encoding="utf-8").strip())
        assert line["requested"] == "2026-07-28"
        assert line["negotiated"] == server.PROTOCOL_VERSION
        assert line["client"] == "zcode" and line["client_version"] == "9.9"
    finally:
        os.environ.pop("UNIFIED_RX_CLIENTS_LOG", None)


def test_handshake_record_failure_never_breaks(tmp_path):
    d = tmp_path / "as_dir"          # 落点是个目录 → 写入必失败
    d.mkdir()
    os.environ["UNIFIED_RX_CLIENTS_LOG"] = str(d)
    try:
        r = server._handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"}})
        assert r["result"]["protocolVersion"] == "2025-03-26"
    finally:
        os.environ.pop("UNIFIED_RX_CLIENTS_LOG", None)


def test_audit_copy_dirty_guard(tmp_path):
    dest = str(tmp_path / "audit-copy")
    script = os.path.join(ROOT, "scripts", "audit_copy.py")
    ok = _py(script, dest, "--allow-dirty")
    assert ok.returncode == 0 and "AUDIT-COPY OK" in ok.stdout, ok.stderr
    st = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=60)
    if (st.stdout or "").strip():        # 开发中必然是脏树 → 不带标志必须拒
        cp2 = _py(script, dest + "2")
        assert cp2.returncode != 0 and "未提交" in cp2.stderr
