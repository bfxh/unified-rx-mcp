# -*- coding: utf-8 -*-
"""S148 契约：sys 域（混合架构调度）——拓扑分级 / 线程视图 / steer 授权与实效。

设计口径：
- 拓扑：P/E 分级必须自洽（classes 的核数和 == physical_cores），且两 API
  （GLPIEx vs CPU Sets）对 P 逻辑核的判定一致（实现内已交叉，这里锁形状）；
- 线程：对自己的进程枚举必须有线程；priority/ideal 字段在册；
- steer：**授权门**（无 __authorized 必拒）；实效用"自建靶进程"验证——
  子进程经 **stdout** 回传 PID（不落文件，避免写盘面）→ steer background →
  优先级 -1 且 CPU 集非空 → steer render → 优先级 1 → 清理。
  全链走 MCP registry（与宿主同路径）。
"""
import os
import subprocess
import sys
import time

import pytest

import registry
import tools  # noqa: F401


def _exe_missing():
    from tools.appaudit import _rs_exe
    return _rs_exe("rx-sys.exe") is None


pytestmark = pytest.mark.skipif(_exe_missing(), reason="rx-sys.exe 未构建")


def test_topology_shape_and_class_consistency():
    r = registry.call("sys_topology", {})
    assert r.get("ok"), r
    res = r["result"]
    assert res["vendor"], "厂商串必须非空"
    assert res["logical_total"] >= 1
    cs = res["cores"]
    assert len(cs) == res["physical_cores"]
    tot_logical = sum(len(c["logical"]) for c in cs)
    assert tot_logical == res["logical_total"], "逻辑核展开数须等于系统逻辑核数"
    per_class = {}
    for c in cs:
        per_class[c["class"]] = per_class.get(c["class"], 0) + 1
    declared = {e["name"]: e["cores"] for e in res["classes"]}
    assert per_class == declared, f"分级自洽失败: {per_class} vs {declared}"
    # 双 API 交叉：P 级 CPU 集逻辑核集合 == GLPIEx 的 P 逻辑核集合
    if not res["uniform"]:
        p_from_cores = {int(x.split(":")[1]) for c in cs if c["class"] == "P"
                        for x in c["logical"]}
        effs = sorted({s["efficiency_class"] for s in res["cpu_sets"]}, reverse=True)
        p_from_sets = {s["logical"] for s in res["cpu_sets"]
                       if s["efficiency_class"] == effs[0]}
        assert p_from_cores == p_from_sets, "两 API 的 P 核判定不一致"


def test_threads_lists_own_process():
    r = registry.call("sys_threads", {"pid": os.getpid()})
    assert r.get("ok"), r
    res = r["result"]
    assert res["count"] >= 1, "自己进程至少一条线程"
    t = res["threads"][0]
    for key in ("tid", "priority", "ideal_number", "cpu_sets"):
        assert key in t, f"线程字段缺 {key}"


def test_steer_requires_auth():
    r = registry.call("sys_steer", {"pid": os.getpid(), "profile": "render"})
    assert r.get("ok") is False
    assert "授权" in (r.get("error") or "")


def test_steer_effective_on_target_process():
    """端到端：自建靶进程（stdout 回传 PID）→ background → render 实效校验。"""
    code = "import os,time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n"
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    try:
        pid = int(proc.stdout.readline().strip())
        assert pid == proc.pid
        topo = registry.call("sys_topology", {})["result"]
        if topo["uniform"]:
            pytest.skip("非混合平台：E/P 定向无区分度（如实跳过）")
        b = registry.call("sys_steer", {"pid": pid, "profile": "background",
                                        "__authorized": True})
        assert b.get("ok"), b
        assert b["result"]["ok_count"] >= 1, f"background 全败: {b['result']}"
        th = registry.call("sys_threads", {"pid": pid})["result"]["threads"]
        assert any(t["priority"] == -1 for t in th), "background 应把优先级压到 -1"
        assert any(t["cpu_sets"] for t in th), "background 应定向到 E 核 CPU 集"
        rr = registry.call("sys_steer", {"pid": pid, "profile": "render",
                                         "__authorized": True})
        assert rr.get("ok") and rr["result"]["ok_count"] >= 1, rr
        th2 = registry.call("sys_threads", {"pid": pid})["result"]["threads"]
        assert any(t["priority"] == 1 for t in th2), "render 应把优先级提到 1"
    finally:
        proc.kill()


def test_devices_shape():
    r = registry.call("sys_devices", {})
    assert r.get("ok"), r
    res = r["result"]
    assert isinstance(res["adapters"], list)
    assert res["count"] == len(res["adapters"])
    names = [a["description"] for a in res["adapters"]]
    assert len(names) == len(set(names)), "同描述适配器必须去重"
