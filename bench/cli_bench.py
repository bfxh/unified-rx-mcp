# -*- coding: utf-8 -*-
"""命令行基准仪（S150）：**不变质量前提下的加速**必须有机器证据。

两件事一起做，缺一不可：
1. **金标准（golden）**：每条代表命令的 stdout + 退出码 存哈希——任何"优化"
   都必须**逐字节同输出**（质量不变是硬约束，不是形容词）；
2. **计时（bench）**：每条命令 N 次取中位数——加速要看得见，回归要拦得住。

用法：
  python -X utf8 bench/cli_bench.py --golden          # 采集金标准（优化前跑）
  python -X utf8 bench/cli_bench.py --check-golden    # 校验输出未变（优化后必跑）
  python -X utf8 bench/cli_bench.py --bench           # 采集计时基线
  python -X utf8 bench/cli_bench.py --check           # 计时对照基线（>1.25× 即红）
  python -X utf8 bench/cli_bench.py --all             # 上面四步一次跑完

落盘：spec/cli-golden.json（输出指纹）/ spec/cli-baseline.json（计时）。
自给自足：夹具建在 %TEMP%/urx-cli-bench（沙盒只放行夹具根，实测命令均在其内）。
调用纪律：命令以「程序 + 参数」分参传入，调用点保持**字面量列表**且 shell=False。
"""
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GOLDEN = ROOT / "spec" / "cli-golden.json"
BASE = ROOT / "spec" / "cli-baseline.json"
# 夹具路径**固定**（金标准要求跨运行同输入；mkdtemp 每次换路径=输出含路径就假红）
W = (Path(tempfile.gettempdir()) / "urx-cli-bench-fx").resolve()
shutil.rmtree(W, ignore_errors=True)
W.mkdir(parents=True, exist_ok=True)
os.environ["UNIFIED_RX_SANDBOX"] = str(W)      # 夹具根即可（命令都在夹具内跑）


def _p(rel):
    q = (W / rel).resolve()
    if W not in q.parents and q != W:
        raise ValueError(f"夹具路径越界: {rel}")
    return q


def _w(rel, text):
    q = _p(rel)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(text, encoding="utf-8")
    return str(q)


def build_fixture():
    _p("fx").mkdir(parents=True, exist_ok=True)
    _w("fx/a.py", "def alpha(x):\n    y = x + 1\n    return y\n\n\n"
                  "def dead_one():\n    return 42\n")
    _w("fx/b.py", "import a\n\n\ndef beta():\n    return a.alpha(2)\n")
    _w("fx/notes.md", "# notes\n\nhello bench\n")


def exe(name):
    t = os.environ.get("TEMP", r"C:\Temp")
    for kind in ("release", "debug"):
        cand = os.path.join(t, "rx-rs-target", kind, name)
        if os.path.isfile(cand):
            return cand
    return name


# 不进金标准的命令（仍计时，但不做逐字节承诺）——两类"按设计会变"：
#   sys_procs  = 活进程列表（环境相关，两次运行就可能不同）
#   mcp_version= 版本回显（每次发版必变，S150 实锤：2.65.0→2.66.0 触发假红）
_NONDET = {"sys_procs", "mcp_version"}


def commands():
    """代表命令集：(程序, *参数)。覆盖 sys/fs/scan/search/ide/taint 六域，全在夹具内。"""
    f = str(_p("fx"))
    a_py = str(_p("fx/a.py"))
    return {
        "sys_topology": (exe("rx-sys.exe"), "topology"),
        "sys_devices": (exe("rx-sys.exe"), "devices"),
        "sys_procs": (exe("rx-sys.exe"), "procs", "python"),
        "fs_list": (exe("rx-fs.exe"), "list", f),
        "scan_secrets": (exe("rx-scan.exe"), "secrets", f),
        "scan_bug": (exe("rx-scan.exe"), "bug", f),
        "search_query": (exe("rx-search.exe"), "query", f, "alpha"),
        "semantic": (exe("rx-semantic.exe"), "search", f, "alpha"),
        "ide_outline": (exe("rx-ide.exe"), "outline", a_py),
        "taint_scan": (exe("rx-taint.exe"), "scan", f),
        "mcp_version": (exe("rx-mcp.exe"), "--version"),
    }


def fingerprint(prog, *extra):
    """输出指纹（rc + stdout 哈希 + 字节数）：质量不变的机器证据。"""
    cp = subprocess.run([prog, *extra], capture_output=True, timeout=300,
                        shell=False)
    h = hashlib.sha256(cp.stdout or b"").hexdigest()[:16]
    return {"rc": cp.returncode, "sha": h, "out_bytes": len(cp.stdout or b"")}


def timeit(prog, *extra, n=9):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run([prog, *extra], capture_output=True, timeout=300,
                       shell=False)
        ts.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(ts), 2),
            "min_ms": round(min(ts), 2)}


def host_key():
    """机器指纹：金标准/计时是**机器本地证据**（同机同 exe 版本对账）。
    跨机器（CI runner）不可比——夹具绝对路径与硬件都不同，必须显式 SKIP
    而不是假红（S150 实锤：CI 上 out_bytes 243 vs 249）。"""
    return f"{platform.node()}|{Path(tempfile.gettempdir()).resolve()}"


def main(argv):
    build_fixture()
    cmds = commands()
    want_all = "--all" in argv
    force = "--force" in argv
    rc = 0
    if "--golden" in argv or want_all:
        doc = {k: fingerprint(*v) for k, v in cmds.items() if k not in _NONDET}
        GOLDEN.write_text(json.dumps({"host": host_key(), "commands": doc},
                                     ensure_ascii=False, indent=1),
                          encoding="utf-8")
        print(f"GOLDEN 已采集（{len(doc)} 条）->", GOLDEN.name)
    if "--bench" in argv or want_all:
        doc = {k: timeit(*v) for k, v in cmds.items()}
        BASE.write_text(json.dumps({"host": host_key(), "commands": doc},
                                   ensure_ascii=False,
                                   indent=1), encoding="utf-8")
        tot = sum(v["median_ms"] for v in doc.values())
        print(f"BASELINE 已采集（{len(doc)} 条，合计中位 {tot:.0f}ms）->", BASE.name)
    if "--check-golden" in argv or want_all:
        if not GOLDEN.is_file():
            print("GOLDEN 缺基线（先 --golden）")
            return 2
        gdoc = json.loads(GOLDEN.read_text(encoding="utf-8"))
        if gdoc.get("host") != host_key() and not force:
            print(f"CLI-GOLDEN SKIP（异机基线：{gdoc.get('host')} ≠ 本机）"
                  "——金标准为机器本地证据，重采用 --golden")
            return 0
        old = gdoc["commands"]
        bad = []
        for k, v in cmds.items():
            if k in _NONDET:
                continue
            now = fingerprint(*v)
            if old.get(k) != now:
                bad.append(f"{k}: {old.get(k)} != {now}")
        checked = len([k for k in cmds if k not in _NONDET])
        print(f"CLI-GOLDEN {'OK' if not bad else 'FAIL'} 比对 {checked} 条（{len(_NONDET)} 条天然不确定已排除）")
        for b in bad:
            print("  ", b)
        rc = 1 if bad else rc
    if "--check" in argv or want_all:
        if not BASE.is_file():
            print("BASELINE 缺基线（先 --bench）")
            return 2
        bdoc = json.loads(BASE.read_text(encoding="utf-8"))
        if bdoc.get("host") != host_key() and not force:
            print(f"CLI-BENCH SKIP（异机基线：{bdoc.get('host')} ≠ 本机）")
            return 0
        old = bdoc["commands"]
        slow = []
        for k, v in cmds.items():
            now = timeit(*v)
            o = old.get(k, {})
            # 对照口径用 min_ms（机器负载下的中位数不稳，min=硬件极限更可复现）
            if o.get("min_ms") and now["min_ms"] > o["min_ms"] * 1.25:
                slow.append(f"{k}: min {o['min_ms']} → {now['min_ms']}ms")
        print(f"CLI-BENCH {'OK' if not slow else 'FAIL'} 对照 {len(cmds)} 条（阈值 1.25×）")
        for s in slow:
            print("  ", s)
        rc = 1 if slow else rc
    if not any(a in argv for a in ("--golden", "--bench", "--check",
                                   "--check-golden", "--all")):
        print("用法: --golden | --check-golden | --bench | --check | --all")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        pass   # 固定夹具保留（下次运行开始时重建）
