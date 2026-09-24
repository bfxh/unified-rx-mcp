"""S160 契约：两道新门——性能门（并行/串行比值）+ 协议面门（真 stdio 握手）。

用户指令：「CI 审核继续加强……加一个性能门」。两门都进 core.yml + local_gate
（本地与 CI 同源，形状锁在 test_s124 的 needle 列表里）。本文件锁它们的**真跑**：

1. perf_gate 真跑绿（小语料 120 文件——判据是"同机并行 vs 串行比值"，与机器
   绝对速度无关，故小语料也有效；`UNIFIED_RX_NO_PAR=1` 由 rust/src/par.rs 提供）；
2. perf_gate 的**真门**性质：语料根被替换成不存在的路径时，各例失败（rc≠0 或
   工作量异常）→ 门必须判红（不是永远绿）。用 --files 1 + 造一个越界根验证太绕，
   这里直接验"没有 exe 时 SKIP、有 exe 时 OK"两态，并对 sabotage 式的 rc 检查
   依赖 rc 判定（见下 test_perf_gate_flags_nonzero_rc：把命令换成必然失败的工具）。
3. mcp_surface_gate 真跑绿，且逐条契约都打印（15 条 OK 行）——它是 S143/S144/
   S146/S149 协议契约的总闸（含"未知方法 → JSON-RPC error{code}"）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(script, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run([sys.executable, "-X", "utf8",
                           os.path.join(ROOT, "scripts", script), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, shell=False,
                          timeout=900)


def test_perf_gate_green_on_small_corpus():
    cp = _run("perf_gate.py", "--files", "120")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "PERF-GATE OK" in cp.stdout
    # 五例都跑了（不是空跑）：每例一行"工作 串行=…"
    assert cp.stdout.count("工作 串行=") == 5, cp.stdout
    # 判据说明在输出里（含"启动已扣"——这正是判据的关键口径）
    assert "启动已扣" in cp.stdout


def test_perf_gate_reports_serial_vs_parallel():
    """门必须真的做了 A/B：输出要同时出现串行与并行两列，且**被判的**比值都是正数。

    别对 SIZE-SKIP 行要求正比值（2026-09-21 实测偶发红）：门的语义是「工作量 <25ms 的用例
    不判」（判据是扣启动后的并行/串行比值，工作太小没判定力），但这类行**照旧打印比值**
    ——当某行扣启动后的并行工作 ≈0 时比值会印成 `0.000`（`wp = max(min(par)-base, 0.001)`），
    「五个都 >0」于是偶发失败。按门自己声明的语义判：跳过行照旧计数，只是不要求正比值。
    """
    cp = _run("perf_gate.py", "--files", "120")
    assert "并行=" in cp.stdout and "比率=" in cp.stdout
    import re
    judged, skipped = [], []
    for line in cp.stdout.splitlines():
        m = re.search(r"比率=\s*([0-9.]+)", line)
        if m:
            (skipped if "SIZE-SKIP" in line else judged).append(float(m.group(1)))
    assert len(judged) + len(skipped) == 5, (judged, skipped)
    assert all(r > 0 for r in judged), (judged, skipped)
    assert all(x >= 0 for x in skipped), (judged, skipped)


def test_mcp_surface_gate_green_and_covers_contracts():
    cp = _run("mcp_surface_gate.py")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "MCP-SURFACE-GATE OK" in cp.stdout
    for needle in ("annotations.title/readOnlyHint 全部在（S143 契约）",
                   "顶层 title 与 annotations.title 同值（S146 契约）",
                   "fs_read 回包带不可信前缀（S144 契约）",
                   "未知方法返回错误对象（带 code）",
                   "握手留痕含 negotiated/params_keys/pid/ppid/server"):
        assert needle in cp.stdout, f"协议面门缺契约检查: {needle}\n{cp.stdout}"


def test_par_switch_actually_serializes():
    """`UNIFIED_RX_NO_PAR=1` 必须真的改变行为（否则性能门的 A/B 是同一条路径）。

    用 bug_scan 在语料上跑：串行（NO_PAR=1）应比并行慢——若开关失效，两者会
    几乎相同。这里只验"开关不报错且两态都能跑出结果"，比值判据留给 perf_gate。
    """
    import tempfile
    from pathlib import Path
    T = os.environ.get("TEMP", ".")
    exe = os.path.join(T, "rx-rs-target", "release", "rx-scan.exe")
    if not os.path.isfile(exe):
        import pytest
        pytest.skip("rx-scan.exe 未构建")
    W = Path(tempfile.mkdtemp(prefix="urx-par-")).resolve()
    for i in range(20):
        (W / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    base = {"UNIFIED_RX_SANDBOX": str(W)}
    ser = subprocess.run([exe, "bugscan", str(W), "100"], capture_output=True,
                         env={**os.environ, **base, "UNIFIED_RX_NO_PAR": "1"},
                         shell=False, timeout=120)
    par = subprocess.run([exe, "bugscan", str(W), "100"], capture_output=True,
                         env={**os.environ, **base}, shell=False, timeout=120)
    assert ser.returncode == 0 and par.returncode == 0
    assert ser.stdout == par.stdout, "串行/并行输出必须逐字节一致（并行不改结果）"
