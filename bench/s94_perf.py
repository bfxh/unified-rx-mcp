# -*- coding: utf-8 -*-
"""s94_perf —— S94 质量体检：L2 延迟预算复测 + 内存基线/泄漏 soak（用户：
「不希望 内存 性能 架构 不行，必须在标准上」——先有数，才谈达标）。

延迟口径（EVAL L2 预算行，实测对比）：
- fs_stat <10ms（预算含 rx-fs.exe 进程拉起——Windows CreateProcess 本身数毫秒）
- ast_scan 全仓 ≤2s/100 文件
- engine_query ≤15s（含 BM25 降级）
- 另附 code_search / code_semantic 现值（S77-S79 Rust 收益台账复测，无门禁只记录）
每项：预热 3 轮后计时 N 轮，报 min/p50/max；门禁行超预算 → 本脚本退出码 1。

内存口径：ctypes GetProcessMemoryInfo 取当前进程 WorkingSetSize（纯 stdlib；
Linux 回落 /proc/self/status VmRSS）。基线前 gc.collect()（量「留存」不量
「瞬时垃圾」），混合负载 soak SOAK_ROUNDS 轮（fs/code_search/ast_scan 轮询），
每轮采样；final-baseline ≤ LEAK_MB 视为「无明显泄漏信号」，超限标「疑似泄漏」。

语料：临时目录 100 个 .py（含部分 eval/裸 except 等可扫模式），结束即删。
结果：stdout 人读表 + 追加写 bench/results/s94_perf.json（历史留档）。
"""
import ctypes
import gc
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# bench 直连工具层：显式全开沙盒（仅本进程；bench 不是安全评测，语料在临时目录）
os.environ.setdefault("UNIFIED_RX_SANDBOX", "*")

ROUNDS = {"fs_stat": 100, "code_search": 20, "code_semantic": 10,
          "ast_scan": 5, "engine_query": 3}
SOAK_ROUNDS = 15
LEAK_MB = 50

# EVAL L2 门禁行：工具 → (字段, 预算 ms, 口径说明)
_BUDGETS = {
    "fs_stat": (10, "p50 <10ms"),
    "ast_scan": (2000, "max ≤2s/100 文件"),
    "engine_query": (15000, "max ≤15s（含 BM25 降级）"),
}


class _PMEX(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def rss_bytes():
    """当前进程工作集字节数（Windows psapi / Linux procfs），失败返回 None。

    Windows 侧两坑（S94 探针实锤）：PROCESS_MEMORY_COUNTERS 若漏 WorkingSetSize
    字段布局即错、API 拒收；GetCurrentProcess 伪句柄必须 restype=c_void_p +
    argtypes 显式声明，默认 int 转换会把 -1 伪句柄传坏。
    """
    if os.name == "nt":
        pmex = _PMEX()
        pmex.cb = ctypes.sizeof(_PMEX)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PMEX), ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(pmex), pmex.cb):
            return None
        return pmex.WorkingSetSize
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return None


def _timed(fn, rounds):
    """预热 3 轮后计时 rounds 轮，返回 {min,p50,max,n}（毫秒）。"""
    for _ in range(3):
        fn()
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return {"min": round(ts[0], 1), "p50": round(ts[len(ts) // 2], 1),
            "max": round(ts[-1], 1), "n": rounds}


def make_corpus():
    """100 个小 .py：常规代码 + 少量 eval/裸 except 模式（给 ast_scan 有活干）。"""
    root = tempfile.mkdtemp(prefix="s94_corpus_")
    sub = os.path.join(root, "pkg")
    os.makedirs(sub)
    for i in range(100):
        body = [f"# 模块 {i}\n", "import os\n", "\n"]
        for j in range(40):
            body.append(f"def fn_{i}_{j}(x):\n    return x * {j} + os.path.sep\n")
        if i % 10 == 0:  # 每 10 个文件埋一个可扫模式
            body.append("eval('1+1')\ntry:\n    pass\nexcept:\n    pass\n")
        d = sub if i % 2 else root
        with open(os.path.join(d, f"mod_{i:03d}.py"), "w",
                  encoding="utf-8") as f:
            f.write("".join(body))
    return root


def run():
    import tools  # noqa: F401  注册 57 工具
    import registry

    corpus = make_corpus()
    one_file = os.path.join(corpus, "mod_000.py")
    try:
        out = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "latency": {}, "memory": {}, "budget_fail": []}

        def call(name, args):
            r = registry.call(name, dict(args))
            if not r.get("ok"):
                raise RuntimeError(f"{name} 调用失败: {r.get('error')}")
            return r["result"]

        probes = {
            "fs_stat": (lambda: call("fs_stat", {"path": one_file}),
                        {"path": one_file}),
            "code_search": (lambda: call("code_search", {
                "query": "乘法函数", "root": corpus, "k": 10}),
                {"query": "乘法函数", "root": corpus, "k": 10}),
            "code_semantic": (lambda: call("code_semantic", {
                "query": "fn_3_7", "root": corpus, "mode": "search"}),
                {"query": "fn_3_7", "root": corpus, "mode": "search"}),
            "ast_scan": (lambda: call("ast_scan", {
                "path": corpus, "max_files": 100}),
                {"path": corpus, "max_files": 100}),
            "engine_query": (lambda: call("engine_query", {
                "query": "读取文件", "root": corpus, "limit": 10}),
                {"query": "读取文件", "root": corpus, "limit": 10}),
        }
        for name, (fn, _) in probes.items():
            r = _timed(fn, ROUNDS[name])
            out["latency"][name] = r
            if name in _BUDGETS:
                budget, desc = _BUDGETS[name]
                gate = "PASS" if (r["p50"] if name == "fs_stat" else r["max"]) \
                    < budget else "FAIL"
                out["latency"][name]["budget"] = f"{desc} -> {gate}"
                if gate == "FAIL":
                    out["budget_fail"].append(name)

        # ---- 内存基线 + 泄漏 soak：先把延迟探针当负载跑 SOAK_ROUNDS 轮 ----
        gc.collect()
        base = rss_bytes()
        series = []
        if base is not None:
            order = ["fs_stat"] * 10 + ["code_search", "ast_scan"]
            for _ in range(SOAK_ROUNDS):
                for name in order:
                    probes[name][0]()
                series.append(rss_bytes())
            gc.collect()
            final = rss_bytes()
            delta_mb = (final - base) / (1 << 20)
            out["memory"] = {
                "baseline_mb": round(base / (1 << 20), 1),
                "final_mb": round(final / (1 << 20), 1),
                "delta_mb": round(delta_mb, 1),
                "peak_mb": round(max(s for s in series if s) / (1 << 20), 1),
                "soak_rounds": SOAK_ROUNDS,
                "verdict": "疑似泄漏(>%.0fMB)" % LEAK_MB if delta_mb > LEAK_MB
                           else "无明显泄漏信号",
            }
        return out
    finally:
        shutil.rmtree(corpus, ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out = run()
    print(f"S94 PERF @ {out['ts']}  (预算: fs_stat p50<10ms / "
          f"ast_scan max≤2s/100f / engine_query max≤15s)")
    for name, r in out["latency"].items():
        line = (f"  {name:<14} min={r['min']:>8} p50={r['p50']:>8} "
                f"max={r['max']:>8} ms  n={r['n']}")
        if "budget" in r:
            line += f"   [{r['budget']}]"
        print(line)
    m = out["memory"]
    if m:
        print(f"  内存基线 {m['baseline_mb']}MB -> soak{m['soak_rounds']}轮 -> "
              f"{m['final_mb']}MB (Δ={m['delta_mb']}MB, 峰值 {m['peak_mb']}MB) "
              f"→ {m['verdict']}")
    else:
        print("  内存: RSS 采样不可用（跳过 soak 判定）")
    dst = os.path.join(HERE, "results", "s94_perf.json")
    hist = []
    if os.path.exists(dst):
        try:
            with open(dst, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = []
    hist.append(out)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(hist[-20:], f, ensure_ascii=False, indent=1)
    print(f"  结果追加 → bench/results/s94_perf.json（保留最近20条）")
    return 1 if out["budget_fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
