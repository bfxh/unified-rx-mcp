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
  python -X utf8 bench/cli_bench.py --check           # 计时对照基线（**同轮比值** >1.25× 即红）
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
#   sys_devices = 显示适配器清单（虚拟显示口随远控软件启停增减，S151 实锤 359 vs 447 字节）
_NONDET = {"sys_procs", "mcp_version", "sys_devices"}


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


# 计时判据的**同轮基准**：`mcp_version` 是纯进程启动成本（不读语料、不做活），
# 拿它当分母，机器级漂移在分子分母里同量抵消。
BENCH_REF = "mcp_version"


def env_stamp(cmds):
    """记录用指纹（**不参与 SKIP 判定**，避免「指纹一变门就静默失效」）：
    解释器版本 + 被测 exe 的构建戳 + 采集时间。
    2026-09-21 实测教训：只记 host 时，「同机同码、机器状态漂了」与「环境真的变了」
    无法区分——归因花掉的时间就白花。写进记录里，一眼分开。"""
    exes = sorted({v[0] for v in cmds.values()})
    stamp = []
    for e in exes:
        p = Path(e)
        try:
            st = p.stat()
            stamp.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            stamp.append(f"{p.name}:missing")
    return {"python": platform.python_version(),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "exes": stamp}


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
        BASE.write_text(json.dumps({"host": host_key(), "env": env_stamp(cmds), "commands": doc},
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
            # 异机 = 显式 SKIP；不 return（后面的计时段仍要跑/也要能各自 SKIP）
            print(f"CLI-GOLDEN SKIP（异机基线：{gdoc.get('host')} ≠ 本机）"
                  "——金标准为机器本地证据，重采用 --golden")
        else:
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
        else:
            old = bdoc["commands"]
            ref_old = old.get(BENCH_REF, {}).get("min_ms")
            if not ref_old:
                print(f"CLI-BENCH SKIP（基线缺同轮基准 {BENCH_REF}，重采 --bench）")
                return 2
            now_t = {k: timeit(*v) for k, v in cmds.items()}
            ref_now = now_t[BENCH_REF]["min_ms"]
            slow, drift = [], []
            for k in cmds:
                o, now = old.get(k, {}), now_t[k]
                if not o.get("min_ms"):
                    continue
                # **主判据：同轮比值**（分量、分母在同一轮里采，机器级漂移同量抵消）。
                # 绝对基线只作参考：2026-09-21 实测同机同码曾整体漂 1.25–1.40×，
                # 连平凡命令一起漂 ⇒ 用绝对数判红绿等于在判机器状态（本仓 perf_gate 早有同款教训）。
                if k != BENCH_REF and now["min_ms"] / ref_now > (o["min_ms"] / ref_old) * 1.25:
                    slow.append(f"{k}: 比值 {o['min_ms'] / ref_old:.3f} → "
                                f"{now['min_ms'] / ref_now:.3f}"
                                f"（{o['min_ms']}→{now['min_ms']}ms；同轮参考 {ref_old}→{ref_now}ms）")
                elif now["min_ms"] > o["min_ms"] * 1.25:
                    drift.append(f"{k}: 绝对 {o['min_ms']} → {now['min_ms']}ms")
            print(f"CLI-BENCH {'OK' if not slow else 'FAIL'} 对照 {len(cmds)} 条"
                  f"（判据：同轮比值 vs 基线比值 ×1.25，基准 = {BENCH_REF}）")
            for s in slow:
                print("  ", s)
            if drift:
                print(f"  NOTE 绝对基线漂移（机器状态，不阻断）：{len(drift)} 条")
                for d in drift[:4]:
                    print("    ", d)
            stamp_old, stamp_now = bdoc.get("env", {}), env_stamp(cmds)
            for field in ("python", ):
                if stamp_old.get(field) and stamp_old[field] != stamp_now[field]:
                    print(f"  NOTE 环境变化：{field} {stamp_old[field]} → {stamp_now[field]}"
                          f"（基线 {stamp_old.get('recorded_at', '?')} 采；变了就该考虑重采）")
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
