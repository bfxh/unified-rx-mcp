#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""开发中检查（写完即验——用户要求：写代码时必须带检查，不许写完不查）。

用法：
    python scripts/dev_check.py                    # 检查当前 git 变更文件
    python scripts/dev_check.py path/to/file.py    # 指定文件（写完立刻验）
    python scripts/dev_check.py --quick            # 只扫变更文件（不跑全量测试）

为什么有这个工具（2026-08-20 用户批评）：
  "每次开发的阶段为什么会有大量的bug 不就是开发阶段 还有大量的问题
   如果你开发 写代码的时候没有检查 那就会给我大量的问题"
  根因：此前规则是"改完必跑"（事后检查）——写完一大坨才验，bug 早积累。
  本工具把检查下沉到"写完一个代码单元立刻验"：
    ① 语法检查（node --check / python -m py_compile / go vet 按扩展名）
    ② bug_scan 增量扫变更文件（复用 server 生产路径）
    ③ 相关测试自动发现（变更文件名 → 匹配 test_*.py 测试）
    ④ 语义回归（--quick 可跳过）
  退出码：0 = 全过；1 = 有检查失败。

用法示例（写完 skin_deform 立刻）：
    python scripts/dev_check.py geometry_tools.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.environ.setdefault("UNIFIED_RX_SANDBOX", REPO)

FAILED = []


def _report(tool: str, ok: bool, detail: str) -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {tool}: {detail[:200]}")
    if not ok:
        FAILED.append((tool, detail))


def _syntax_check(path: str) -> bool:
    """按扩展名做语法检查（写完一个文件立刻验语法——最便宜的检查）。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".py":
            r = subprocess.run([sys.executable, "-m", "py_compile", path],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            _report(f"语法 {os.path.basename(path)}", ok,
                    r.stderr.strip()[:200] if not ok else "")
            return ok
        if ext in (".js", ".mjs", ".cjs"):
            r = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            _report(f"语法 {os.path.basename(path)}", ok,
                    r.stderr.strip()[:200] if not ok else "")
            return ok
        if ext == ".go":
            r = subprocess.run(["go", "vet", path],
                               capture_output=True, text=True, timeout=120)
            ok = r.returncode == 0
            _report(f"go vet {os.path.basename(path)}", ok,
                    r.stderr.strip()[:200] if not ok else "")
            return ok
    except (OSError, subprocess.TimeoutExpired) as exc:
        _report(f"语法 {os.path.basename(path)}", False, f"{type(exc).__name__}")
        return False
    return True


def _bug_scan_files(files: list) -> None:
    """复用 server 生产路径扫变更文件（零额外依赖）。

    误报白名单（2026-08-20 人工确认过）：geometry_tools 的 ms[0]/ms[1]
    参数索引有 len(paths)!=2 入口校验、dedup=[hits[0]] 有 if not hits 保护——
    静态规则无法跨函数看校验，这些是确定性误报。dev_check 对白名单内的
    已知误报行降级为提示（不红），防止"狼来了"导致开发者整体忽略。
    """
    import server
    # (文件名, 行内容包含, 规则) 已知误报——人工确认过有保护（行号会漂移，
    # 用行内容特征匹配更稳；全部在 mesh_boolean 入口 len(paths)!=2 校验保护下）
    KNOWN_FALSE_POSITIVES = [
        ("geometry_tools.py", "dedup = [hits[0]]", "index_out_of_range"),
        ("geometry_tools.py", "ms[0]", "index_out_of_range"),
        ("geometry_tools.py", "ms[1]", "index_out_of_range"),
    ]
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".py", ".rs", ".go", ".ts", ".js", ".tsx", ".jsx", ".gd",
                       ".c", ".cpp", ".h", ".hpp", ".cc", ".cs", ".lua", ".sh",
                       ".java", ".kt", ".rb", ".php", ".dart"):
            continue
        r = server._call("bug_scan", {"path": f})[0].text
        try:
            d = json.loads(r)
            base = os.path.basename(f)
            lines_map = {}
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for i, ln in enumerate(fh, 1):
                        lines_map[i] = ln
            except OSError:
                pass
            errs = [i for i in d.get("issues", []) if i.get("severity") == "error"]
            real = []
            for i in errs:
                ln_txt = lines_map.get(i.get("line"), "")
                is_fp = any(b == base and needle in ln_txt and rule == i.get("rule")
                            for (b, needle, rule) in KNOWN_FALSE_POSITIVES)
                if not is_fp:
                    real.append(i)
            if real:
                _report(f"bug_scan {base}", False,
                        f"{len(real)} error: "
                        + "; ".join(f"L{i['line']} {i['rule']}" for i in real[:5]))
            else:
                _report(f"bug_scan {base}", True,
                        f"issues={d.get('issue_count')}"
                        + (f"（{len(errs)} 已知误报降级）" if errs else ""))
        except json.JSONDecodeError:
            _report(f"bug_scan {os.path.basename(f)}", False, r[:100])


def _discover_tests(files: list) -> list:
    """变更文件 → 相关测试自动发现（test_<基名>.py 或 test_unified_rx.py 中
    含基名的测试）。写完一个单元只跑相关测试——不等全量。"""
    tests = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        cand = os.path.join(REPO, f"test_{base}.py")
        if os.path.isfile(cand) and cand not in tests:
            tests.append(cand)
    # 变更核心模块 → 主测试文件
    if not tests and any(b in ("server", "engine", "guard_core") for b in
                         [os.path.basename(f) for f in files]):
        t = os.path.join(REPO, "test_unified_rx.py")
        if os.path.isfile(t):
            tests.append(t)
    return tests


def _run_tests(tests: list, quick: bool) -> None:
    if not tests:
        return
    cmd = [sys.executable, "-m", "pytest", "-q", *tests]
    if quick:
        cmd.append("--no-header")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        ok = r.returncode == 0
        _report(f"pytest {len(tests)} 文件", ok,
                tail if ok else (r.stdout.strip()[-300:] or r.stderr.strip()[-300:]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        _report("pytest", False, f"{type(exc).__name__}")


def main() -> int:
    quick = "--quick" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        files = [os.path.abspath(a) for a in args]
    else:
        # git 变更文件
        try:
            r = subprocess.run(["git", "-C", REPO, "diff", "--name-only", "HEAD"],
                               capture_output=True, text=True, timeout=60)
            files = [os.path.join(REPO, ln.strip())
                     for ln in r.stdout.splitlines() if ln.strip()]
            if not files:
                r = subprocess.run(["git", "-C", REPO, "status", "--short"],
                                   capture_output=True, text=True, timeout=30)
                files = [os.path.join(REPO, ln[3:].strip())
                         for ln in r.stdout.splitlines() if ln.strip()]
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] git 变更检测: {exc}")
            return 1
    if not files:
        print("[OK] 无变更文件（工作区干净）")
        return 0
    print(f"== 开发中检查（{len(files)} 个文件，{'quick' if quick else '完整'}）==")
    for f in files:
        if os.path.isfile(f):
            _syntax_check(f)
    _bug_scan_files(files)
    tests = _discover_tests(files)
    if tests:
        _run_tests(tests, quick)
    if not quick:
        r = subprocess.run([sys.executable, "scripts/semantic_regression.py"],
                           capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0
        _report("语义回归", ok,
                (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")[:200])
    if FAILED:
        print(f"\n[FAIL] 开发中检查失败 {len(FAILED)} 项——先修再继续（不许带病提交）")
        for tool, detail in FAILED:
            print(f"   - {tool}: {detail[:150]}")
        return 1
    print("\n[OK] 开发中检查全过——可以继续下一个单元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
