# -*- coding: utf-8 -*-
"""tools/ide_doctor.py —— 一键项目体检（R4，vf3_battery 脱硬编码成通用工具）。

一次调用聚合六个既有检查：bug_scan / code_review / build / test / dep_graph /
module_stability，统一 JSON 报告 + top 问题清单——任何仓库一条命令出基线。
如实定界：纯聚合不造新检测；单项深度仍用各自工具。
"""
import os
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve


def _run_check(name, fn):
    t0 = time.time()
    try:
        r = fn()
        if isinstance(r, dict) and r.get("ok") is False:
            if isinstance(r.get("result"), dict):
                # 工具自己报的失败（带详情）——区别于基础设施崩溃
                return {"check": name, "status": "failed", "result": r["result"],
                        "elapsed_s": round(time.time() - t0, 2)}
            return {"check": name, "status": "error",
                    "summary": str(r.get("error"))[:160],
                    "elapsed_s": round(time.time() - t0, 2)}
        res = r.get("result") if isinstance(r, dict) else None
        if res is None:
            err = (r or {}).get("error") if isinstance(r, dict) else str(r)
            return {"check": name, "status": "error", "summary": str(err)[:160],
                    "elapsed_s": round(time.time() - t0, 2)}
        return {"check": name, "status": "ok", "result": res,
                "elapsed_s": round(time.time() - t0, 2)}
    except Exception as e:                                  # noqa: BLE001
        return {"check": name, "status": "error",
                "summary": f"{type(e).__name__}: {e}"[:160],
                "elapsed_s": round(time.time() - t0, 2)}


@tool("ide_doctor", "一键项目体检：bug_scan + code_review + 构建 + 测试 + 依赖环 + "
      "模块稳定性 → 统一报告与 top 问题清单（任何仓库一条命令出基线）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（沙盒内）"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 300）"},
           "run_tests": {"type": "boolean",
                         "description": "是否跑测试（大仓库可关，默认 true）"},
       },
       "required": ["path"]})
def ide_doctor(path, max_files=300, run_tests=True):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}

    def reg(name, args):
        from registry import call
        return call(name, args)

    checks = [
        _run_check("bug_scan", lambda: reg(
            "bug_scan", {"path": path, "max_files": max_files})),
        _run_check("code_review", lambda: reg(
            "code_review", {"path": path, "max_files": max_files})),
        _run_check("build", lambda: reg("ide_build", {"path": path,
                                                      "action": "check"})),
    ]
    if run_tests:
        checks.append(_run_check("test", lambda: reg("ide_test", {"path": path})))
    checks.append(_run_check("dep_graph", lambda: reg("dep_graph", {"path": path})))
    checks.append(_run_check("stability", lambda: reg("module_stability",
                                                      {"path": path})))

    problems = []      # (severity, text)
    warns = []
    by_name = {c["check"]: c for c in checks}

    # bug_scan：definite 是真问题，clue 是线索
    bs = by_name.get("bug_scan", {})
    if bs.get("status") == "ok":
        r = bs["result"]
        if r.get("total"):
            defs = [i for i in (r.get("issues") or [])
                    if i.get("kind") == "definite"]
            if defs:
                problems.extend(f"bug_scan definite: {i['file']}:{i['line']} "
                                f"{i['rule']} {i['msg']}" for i in defs[:5])
            else:
                warns.append(f"bug_scan: {r['total']} 条线索（无 definite）")

    # build：errors>0 或检查本身失败 = 红灯（体检对象坏了不是体检器坏了）
    bd = by_name.get("build", {})
    if bd.get("status") == "ok":
        r = bd["result"]
        n_err = len(r.get("errors") or [])
        if n_err:
            problems.append(f"build: {n_err} 个编译错误")
        elif not r.get("ok", True):
            problems.append(f"build: exit={r.get('exit')}")
    elif bd.get("status") == "failed":
        r = bd.get("result") or {}
        problems.append(f"build: exit={r.get('exit')} "
                        f"({r.get('tool', 'compileall')} 失败)")
    else:
        problems.append(f"build: 检查无法运行（{bd.get('summary', '')[:80]}）")

    # test：失败 = 红灯；没写测试 = 显式黄灯（不是绿灯）
    ts = by_name.get("test", {})
    if ts.get("status") == "ok":
        r = ts["result"]
        if r.get("collected") == 0:
            warns.append("test: 收集到 0 个测试——没写测试本身就是要处理的问题")
        elif r.get("failed"):
            problems.extend(f"test failed: {f['test']}"
                            for f in (r.get("failures") or [])[:5])
    elif ts.get("status") == "error":
        if "未检测到测试设施" in str(ts.get("summary", "")):
            warns.append("test: 未检测到测试设施——没写测试本身就是要处理的问题")
        else:
            warns.append(f"test: {ts['summary']}")

    # dep_graph：环 = 红灯
    dg = by_name.get("dep_graph", {})
    if dg.get("status") == "ok" and dg["result"].get("cycles"):
        problems.append(f"依赖环: {len(dg['result']['cycles'])} 个 "
                        f"({'; '.join(dg['result']['cycles'][:2])})")

    # stability：risky = 黄灯
    st = by_name.get("stability", {})
    if st.get("status") == "ok" and st["result"].get("risky_modules"):
        warns.append(f"stability: {len(st['result']['risky_modules'])} 个 risky 模块"
                     f"（无测试覆盖代理）")

    verdict = "issues" if problems else ("warn" if warns else "clean")
    out_checks = []
    for c in checks:
        o = {"check": c["check"], "status": c["status"],
             "elapsed_s": c["elapsed_s"]}
        if c["status"] == "ok":
            r = c["result"]
            keep = {k: r[k] for k in ("total", "files", "by_lens", "exit",
                                      "errors", "tool", "collected", "failed",
                                      "cycles", "risky_modules", "by_stability")
                    if k in r}
            o["summary"] = keep
        else:
            o["summary"] = c.get("summary", "")
        out_checks.append(o)
    return {"path": path, "verdict": verdict,
            "problems": problems[:10], "warns": warns[:10],
            "checks": out_checks,
            "elapsed_s": round(sum(c["elapsed_s"] for c in checks), 2),
            "note": "纯聚合不造新检测——单项深挖用各自工具"}
