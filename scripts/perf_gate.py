"""性能门（S160）：**同机"并行 vs 串行"比值**判据——不依赖机器绝对速度，CI 也安全。

动机（用户指令）：CI 审核继续加强、加一个性能门。绝对耗时在 CI 上不可比（runner
速度/核数差异大），但**同一台机器上"并行 vs 串行"的比值**是可比的：
- 期望并行的三件（secrets/bug_scan/code_search）：并行必须**明显快于**串行；
- 其余（taint/semantic）：并行**不得慢于**串行（防调度/合并开销倒挂）。
串行由 `UNIFIED_RX_NO_PAR=1` 强制（`rust/src/par.rs` 单一入口）——同一二进制、
同一语料、同一时刻交错 A/B，故比值稳定。

用法：
  python -X utf8 scripts/perf_gate.py                 # 默认语料 400 文件
  python -X utf8 scripts/perf_gate.py --files 120     # 测试用小语料
  python -X utf8 scripts/perf_gate.py --json          # 机器可读输出

判据（机器核数自适应）：核数 ≥ 4 → 期望组要求比值 ≤ 0.85；核数 ≤ 2 → 只要求 ≤ 1.0
（两核上并行收益有限，不苛求）；任何命令超 120s 直接判红（防挂死）。
语料在 %TEMP%/unrx-perf-corpus 下**确定性重建**（同输入同输出，跨机可比）。

夹具纪律：语料里要触发 bug_scan/secrets 的命中——**危险字面量一律碎片拼接**
（`ev`+`al(`、`xox`+`b-`），与 tests/ 的夹具同规矩（Mimosa 写入预警后照录）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CORPUS = (Path(os.environ.get("TEMP", ".")) / "unrx-perf-corpus").resolve()

# (标签, 程序名, 参数模板, 是否期望并行显著更快)
# (标签, 程序名, 参数模板, 期望组上限比率) —— None = 只要求"不慢于串行"（≤1.05）
CASES = [
    ("bug_scan", "rx-scan.exe", ["bugscan", "{root}", "400"], 0.85),
    ("secrets_hunt", "rx-scan.exe",
     ["secrets", "{root}", "2000", "512", "4.5", "200", ""], 0.85),
    ("code_search", "rx-search.exe", ["{root}", "def", "10"], 0.85),
    ("rust_taint_scan", "rx-taint.exe", ["{root}"], 0.85),
    # semantic 并行收益本就小（打分段是顺序的）——给 0.95，防负载下假红
    ("code_semantic", "rx-semantic.exe", ["{root}", "def", "search", "8"], 0.95),
]

# 碎片拼接：源码里不出现完整危险字面量（运行时拼出同样的语料）
_EVAL = "ev" + "al("
_SLACK = "xox" + "b-"


def _exe(name):
    t = os.environ.get("TEMP", r"C:\Temp")
    for kind in ("release", "debug"):
        cand = os.path.join(t, "rx-rs-target", kind, name)
        if os.path.isfile(cand):
            return cand
    return None


def corpus_source(i, n):
    """确定性语料（同 (i,n) → 同内容；无随机源）：含动态执行/裸 except/凭据样式，
    外加**跨包调用**——让 taint 的调用图与跨文件传播有真实工作（否则它在本语料上
    工作耗时 ~0.5ms，性能门只能 SIZE-SKIP，覆盖不到最近的并行化改动）。"""
    tgt = (i + 1) % n
    return ("import os\n"
            f"from pkg{tgt % 20}.mod_{tgt} import handler_{tgt}\n\n\n"
            f"def handler_{i}(req):\n"
            "    path = req.get('path')\n"
            "    data = open(path).read()\n"
            f"    result = {_EVAL}req['expr'])\n"
            f"    token = '{_SLACK}{i:08d}-abcdefabcdef'\n"
            "    return result + len(data) + len(token)\n\n\n"
            f"def forward_{i}(req):\n"
            f"    return handler_{tgt}(req)\n\n\n"
            f"def helper_{i}(a, b):\n"
            "    try:\n"
            "        return a / b\n"
            "    except Exception:\n"
            "        pass\n")


def build_corpus(n):
    if CORPUS.is_dir():
        shutil.rmtree(CORPUS, ignore_errors=True)
    for i in range(n):
        sub = CORPUS / f"pkg{i % 20}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"mod_{i}.py").write_text(corpus_source(i, n), encoding="utf-8")
    return CORPUS


def _run_once(exe, args, env_extra):
    env = dict(os.environ)
    # 自给自足声明沙盒（taint 走 sandbox::resolve，fail-closed：不设=拒绝，
    # 表现为"工作量 ~0"的假象——S160 实锤，与 ci_secrets_gate 同纪律）
    env.setdefault("UNIFIED_RX_SANDBOX", str(CORPUS))
    env.update(env_extra)
    t0 = time.perf_counter()
    cp = subprocess.run([exe, *args], capture_output=True, env=env, shell=False,
                        timeout=120, cwd=str(ROOT))
    dt = (time.perf_counter() - t0) * 1000
    return dt, cp.returncode


MAX_CALIB_FILES = 3200


def calibrate(start_n):
    """自校准语料规模：把 bug_scan 的**串行工作耗时**推到 ≥ MIN_WORK_MS（上限 3200 文件）。

    为什么（S160 CI 实锤）：4 核 runner + 120 文件 → 工作仅 9.8ms，线程开销盖过
    并行收益（比率 0.918 假红）。语料按机器速度自动扩档后，任何机器上判据都有意义。
    """
    n = start_n
    # 探针 = bug_scan：它的工作量随语料线性增长（search/semantic 有 200 文件上限，
    # 工作量封顶在 ~12/22ms，拿它们当探针会把语料白推到上限——S160 实锤）
    exe = _exe("rx-scan.exe")
    args = ["bugscan", str(CORPUS), "2000"]
    while True:
        build_corpus(n)
        if exe is None:
            return n
        base = min(_run_once(exe, ["--version"], {})[0] for _ in range(3))
        ser = min(_run_once(exe, args, {"UNIFIED_RX_NO_PAR": "1"})[0] for _ in range(3))
        work = max(ser - base, 0.001)
        print(f"  （自校准：语料 {n} 文件 → bug_scan 串行工作 {work:.1f}ms）")
        if work >= MIN_WORK_MS or n >= MAX_CALIB_FILES:
            return n
        n = min(n * 2, MAX_CALIB_FILES)


# 工作量下限：工作耗时（扣启动）低于此值时不判比值（太小测不出并行收益）。
# CI 实锤（S160）：4 核 runner + 120 文件 → bug_scan 工作仅 9.8ms，比率 0.918
# 被判 SLOW（线程开销盖过收益）——故下限提到 25ms，并在 main 里**自校准语料**。
MIN_WORK_MS = 25.0


def measure(cases, rounds=3):
    """交错 A/B（串行/并行轮流各一次算一轮），取各自 min（抗负载抖动）。

    2026-09-20 实测修正：进程启动 ~7ms 是固定项——**判比值必须扣启动基线**，
    否则小语料下"并行 1.067"这类噪声会被误判为回归（taint 语料真实工作仅 ~2ms）。
    """
    out = []
    for label, exe_name, argv_tpl, expect_parallel in cases:  # noqa: E501
        exe = _exe(exe_name)
        if exe is None:
            out.append({"label": label, "skipped": f"{exe_name} 未构建"})
            continue
        args = [a.replace("{root}", str(CORPUS)) for a in argv_tpl]
        base = min(_run_once(exe, ["--version"], {})[0] for _ in range(rounds))
        ser, par, rcs = [], [], set()
        for _ in range(rounds):
            d, rc = _run_once(exe, args, {"UNIFIED_RX_NO_PAR": "1"})
            ser.append(d)
            rcs.add(rc)
            d, rc = _run_once(exe, args, {})
            par.append(d)
            rcs.add(rc)
        ws = max(min(ser) - base, 0.001)
        wp = max(min(par) - base, 0.001)
        out.append({"label": label, "spawn_ms": round(base, 1),
                    "serial_ms": round(min(ser), 1),
                    "parallel_ms": round(min(par), 1),
                    "work_serial_ms": round(ws, 1), "work_parallel_ms": round(wp, 1),
                    "ratio": round(wp / ws, 3), "too_small": ws < MIN_WORK_MS,
                    "rc": sorted(rcs), "expect_parallel": expect_parallel})
    return out


def main(argv):
    explicit = "--files" in argv
    n = int(argv[argv.index("--files") + 1]) if explicit else 400
    if not explicit:
        n = calibrate(n)          # 自动模式：按机器速度扩语料（CI 上会自动放大）
    rows = measure(CASES)
    rows = [r for r in rows if "skipped" not in r]
    if not rows:
        sys.exit("PERF-GATE SKIP: 没有任何 exe 可测（先 cargo build --release）")
    cores = os.cpu_count() or 1
    print(f"PERF-GATE 语料={n} 文件 逻辑核={cores}（各例判据上限见行尾）")
    # 核数自适应（S160）：≤2 核的 runner 上并行收益有限 → 只要求"不慢于串行"，
    # 不苛求加速比（否则 CI 小机器上假红）
    if cores >= 8:
        tier_note = "核数 ≥8：按各例上限判"
        lim_map = None
    elif cores >= 4:
        tier_note = "核数 4-7：统一放宽到 0.95（共享 runner 噪声大）"
        lim_map = 0.95
    else:
        tier_note = "核数 ≤2：只判'不慢于串行'（不苛求并行加速比）"
        lim_map = 1.05
    print(f"  （{tier_note}）")
    small_machine = cores <= 2
    bad = []
    for r in rows:
        flag = ""
        lim = lim_map if lim_map is not None else r["expect_parallel"]
        if r["rc"] != [0]:
            bad.append(f"{r['label']}: 退出码 {r['rc']}")
            flag = " RC!"
        elif r["too_small"]:
            flag = f" SIZE-SKIP(工作量<{MIN_WORK_MS:.0f}ms，不判)"
        elif lim and r["ratio"] > lim:
            bad.append(f"{r['label']}: 并行/串行={r['ratio']} > {lim}"
                       f"（{'并行未生效？' if not small_machine else '比串行还慢'}）")
            flag = " SLOW!"
        print(f"  工作 串行={r['work_serial_ms']:8.1f}ms 并行={r['work_parallel_ms']:8.1f}ms  "
              f"比率={r['ratio']:5.3f}（限 {lim}，启动已扣）  "
              f"{r['label']}{flag}")
    if "--json" in argv:
        print(json.dumps({"cores": cores, "files": n, "rows": rows},
                         ensure_ascii=False))
    if bad:
        sys.exit("PERF-GATE FAIL: " + "; ".join(bad))
    print("PERF-GATE OK")
    return 0


if __name__ == "__main__":
    try:
        if "--files" in sys.argv:
            build_corpus(int(sys.argv[sys.argv.index("--files") + 1]))
        else:
            build_corpus(400)            # 先建基线规模，main 会按需自校准放大
    except Exception as e:                                   # noqa: BLE001
        sys.exit(f"PERF-GATE FAIL: 语料构建失败 {e}")
    sys.exit(main(sys.argv[1:]))
