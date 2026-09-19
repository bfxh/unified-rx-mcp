# -*- coding: utf-8 -*-
"""S151 契约：常驻服务（stdio）——**不变质量**的加速通道。

用户指令：「命令行需要不变质量的情况下加速」（S150 做构建层，本文件守运行层）。
四锁：
1. **逐字节一致**：同一命令，服务回包 vs CLI stdout **必须逐字节相同**（覆盖
   sys/scan/search/semantic/taint 五域；两份解析各一，防漂移）；
2. **回退不变**：`UNIFIED_RX_SVC=off`（或服务不可用）时行为与报错语义与从前一致；
3. **无泄漏**：`stop()` 后子进程已退出（管道 EOF 自清，不留孤儿）；
4. **不许比 CLI 慢**：服务调用不得是 CLI 的 2 倍以上——S151 实锤过"accept 循环
   sleep 轮询让服务比 CLI 慢 15×"的回归，这条把它钉死在测试里。
"""
import json
import os
import subprocess
import sys
import time

import pytest

import registry
import tools  # noqa: F401
import tools.svc as svc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exe(name):
    from tools.appaudit import _rs_exe
    return _rs_exe(name)


pytestmark = pytest.mark.skipif(_exe("rx-svc.exe") is None,
                                reason="rx-svc.exe 未构建")


@pytest.fixture(autouse=True)
def _clean_svc():
    """套件默认 UNIFIED_RX_SVC=off（conftest）；服务测试显式开，例后复位。"""
    old = os.environ.get("UNIFIED_RX_SVC")
    os.environ["UNIFIED_RX_SVC"] = "on"
    svc.stop()
    yield
    svc.stop()
    if old is None:
        os.environ.pop("UNIFIED_RX_SVC", None)
    else:
        os.environ["UNIFIED_RX_SVC"] = old


def _cli_out(exe, *args):
    cp = subprocess.run([exe, *args], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300,
                        shell=False)
    return cp.stdout.strip()


def test_service_matches_cli_byte_for_byte(tmp_path):
    (tmp_path / "s.py").write_text(
        "def f(p):\n    try:\n        return open(p).read()\n    except:\n        pass\n",
        encoding="utf-8")
    d = str(tmp_path)
    cases = [
        ("sys", ["topology"], "rx-sys.exe", ["topology"]),
        ("scan", ["bugscan", d, "100"], "rx-scan.exe", ["bugscan", d, "100"]),
        ("scan", ["secrets", d, "100"], "rx-scan.exe", ["secrets", d, "100"]),
        ("search", [d, "open", "5"], "rx-search.exe", [d, "open", "5"]),
        ("semantic", [d, "open", "search", "5"], "rx-semantic.exe",
         [d, "open", "search", "5"]),
        ("taint", [d], "rx-taint.exe", [d]),
    ]
    for dom, argv, exe_name, cli_args in cases:
        raw = svc.call(dom, argv)          # 原始字符串比（json 重序列化会改空白）
        assert raw is not None, f"{dom} {argv} 服务不可用（应回退但这里要真跑）"
        rc, out = raw
        assert rc == 0, (dom, argv, out[:200])
        cli = _cli_out(_exe(exe_name), *cli_args)
        assert out == cli, f"{dom} {' '.join(argv)} 服务回包与 CLI stdout 不一致"


def test_cli_and_service_agree_via_registry(tmp_path):
    """**五域**工具两条路（服务开/关）结果一致——走 registry 全链。

    exe 级逐字节（上一条）盖不住"工具层后处理"：S151 实锤 taint 的服务路漏掉
    root/naive/cross 三字段（CLI 路在 Python 侧补的），被 tool-evals 抓出——
    故这里对所有覆盖域的代表工具做**注册表级**开/关比对。
    """
    (tmp_path / "m.py").write_text(
        "def f(p):\n    return open(p).read()\n", encoding="utf-8")
    d = str(tmp_path)
    cases = [
        ("std_check", {"path": d}),
        ("bug_scan", {"path": d}),
        ("secrets_hunt", {"path": d}),
        ("code_search", {"query": "open", "root": d}),
        ("code_semantic", {"query": "open", "root": d}),
        ("rust_taint_scan", {"root": d}),
        ("rust_taint_scan", {"root": d, "naive": True}),
        ("sys_topology", {}),                 # 注：sys_threads/sys_procs 是活列表，
                                              # 天然逐次不同，不进逐字节比对
    ]
    os.environ.pop("UNIFIED_RX_SVC", None)
    svc.stop()
    on = {t: registry.call(t, a) for t, a in cases}
    svc.stop()
    os.environ["UNIFIED_RX_SVC"] = "off"
    try:
        off = {t: registry.call(t, a) for t, a in cases}
    finally:
        os.environ.pop("UNIFIED_RX_SVC", None)
    for t, a in cases:
        assert on[t].get("ok") == off[t].get("ok"), (t, a, on[t], off[t])
        assert on[t].get("ok"), (t, a, on[t])

        def _norm(res):
            # 计时/耗时字段天然每次不同（secrets_hunt.elapsed_ms）——归一后再比
            d = dict(res or {})
            for k in ("elapsed_ms", "duration_ms", "ms"):
                d.pop(k, None)
            return json.dumps(d, ensure_ascii=False, sort_keys=True)

        assert _norm(on[t]["result"]) == _norm(off[t]["result"]), \
            f"{t} {a} 服务/CLI 两路结果不一致"


def test_fallback_when_disabled(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    os.environ["UNIFIED_RX_SVC"] = "off"
    try:
        assert svc.call("sys", ["topology"]) is None, "off 时不应起服务"
        assert svc._proc is None
        r = registry.call("sys_topology", {})
        assert r.get("ok"), r            # 仍走 spawn 路径成功
    finally:
        os.environ.pop("UNIFIED_RX_SVC", None)


def test_service_restarts_after_kill():
    assert svc.call("sys", ["topology"]) is not None
    proc = svc._proc
    assert proc is not None and proc.poll() is None
    proc.kill()
    proc.wait(timeout=5)
    got = svc.json_call("sys", ["topology"])   # 下次调用应自动重起
    assert got is not None and got[0] == 0


def test_stop_leaves_no_orphan():
    assert svc.call("sys", ["topology"]) is not None
    proc = svc._proc
    svc.stop()
    assert proc.poll() is not None, "stop() 后服务必须已退出（无孤儿）"
    assert svc._proc is None


def test_service_not_slower_than_cli():
    """防"服务比 CLI 还慢"的回归（S151 实锤：sleep 轮询 → 151ms/次）。"""
    exe = _exe("rx-sys.exe")
    assert svc.call("sys", ["topology"]) is not None     # 预热

    def med(fn, n=15):
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t0) * 1000)
        return sorted(ts)[len(ts) // 2]

    svc_ms = med(lambda: svc.call("sys", ["topology"]))
    cli_ms = med(lambda: subprocess.run([exe, "topology"], capture_output=True,
                                        shell=False))
    assert svc_ms < cli_ms * 2, \
        f"服务 {svc_ms:.2f}ms 比 CLI {cli_ms:.2f}ms 还慢 2× 以上（回归）"
