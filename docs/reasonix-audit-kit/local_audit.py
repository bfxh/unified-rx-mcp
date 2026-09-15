#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_audit.py —— DeepSeek-Reasonix 本地审核门（提案版，zero-dependency）。

设计参照 unified-rx-mcp 的本地门实践（S145/S147）：**审核一条命令跑完、本地
优先、不依赖远端 CI 与特定 OS**。CI 是镜像/备份；合入前的"最小完整审核"必须在
本机可执行、可复现、可判红。

用法（仓库根）：
    python3 local_audit.py            # 全门：快门 + go test ./...（慢档）
    python3 local_audit.py --fast     # 快门：repolint / vet / lint / 明文 / 依赖 / 大文件 / 时效
    python3 local_audit.py --list     # 列出步骤
    python3 local_audit.py --only secrets,deps

步骤与取舍：
  repolint     tools/repolint（仓内既有）        —— 你们已有，保留为第一步
  vet          go vet ./...                      —— 快、跨平台
  golangci     golangci-lint run                 —— 缺失则 SKIP（不静默当通过）
  secrets      工作树明文红线（正则，碎片拼接）    —— 零依赖内联实现
  history      近 N 提交 diff 明文（git log -p）  —— 树扫看不到的历史面
  deps         go.mod require 清单对账（--update-deps 刷新基线）
  bigfiles     源文件行数/体积上限（推动上帝文件拆分）
  freshness    审计账本时效（.audit-ledger.json，--allow-stale 放行）
  test         go test ./... -count=1            —— 慢档（全门才跑）

退出码：任一步 FAIL → 1（钩子据此拦截）。
读取纪律：文件 IO 一律经 `_safe()`（resolve 后必须在仓库根内）后 **pathlib
直读直写**（不裸 open）——与 unified-rx-mcp 沙盒纪律同款。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
ROOT = str(_ROOT)
LEDGER = _ROOT / ".audit-ledger.json"
DEPS_BASE = _ROOT / ".deps-baseline.json"

# ---- 阈值（可按仓库实际调整；改阈值=记账动作，写进 commit message）----
MAX_FILE_LINES = 5000          # 单源文件行数上限（desktop/app.go 现状远超）
MAX_FILE_BYTES = 300 * 1024    # 单源文件体积上限
AUDIT_MAX_DAYS = 14
AUDIT_MAX_COMMITS = 60
HISTORY_N = 50

SRC_EXT = (".go", ".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".ps1", ".yml",
           ".yaml", ".toml", ".json", ".md")
SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "testdata",
             ".next", "out", "coverage"}

# ---- 明文面（碎片拼接写法：本文件自身不得含完整模式串）----
SECRET_PATTERNS = [
    ("aws_akid", re.compile(r"\b(AK" + r"IA[0-9A-Z]{16})\b")),
    ("github_pat", re.compile(r"\b(gh" + r"p_[A-Za-z0-9]{36})\b")),
    ("slack_token", re.compile(r"\b(xox" + r"[abps]-[A-Za-z0-9-]{10,60})\b")),
    ("google_api", re.compile(r"\b(AI" + r"za[0-9A-Za-z_\-]{35})\b")),
    ("stripe_key", re.compile(r"\b((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,})\b")),
    ("pem_private", re.compile(r"-----BEGIN [A-Z ]*PRIVATE " + r"KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
]
SECRET_EXEMPT_PREFIX = ("tests/", "test/", "docs/", "release-notes/",
                        "benchmarks/", "site/")


def _safe(p):
    """读取/写入前显式校验：resolve 后必须落在仓库根内（越界即拒）。"""
    q = Path(p).resolve()
    if q != _ROOT and _ROOT not in q.parents:
        raise ValueError(f"路径越界（仓库外）: {p}")
    return q


def _read_text(p):
    return _safe(p).read_text(encoding="utf-8", errors="replace")


def _run(args, timeout=3600):
    t0 = time.time()
    cp = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout,
                        shell=False)
    return cp.returncode == 0, time.time() - t0, (cp.stdout or "") + (cp.stderr or "")


def _walk_sources():
    for base, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for f in files:
            if f.endswith(SRC_EXT):
                yield _safe(Path(base) / f)


def _rel(p):
    return Path(p).relative_to(_ROOT).as_posix()   # as_posix：不触碰字符串替换面


def step_vet(_argv):
    return _run(["go", "vet", "./..."])


def step_golangci(_argv):
    if not shutil.which("golangci-lint"):
        return None, 0.0, "golangci-lint 未安装（SKIP——不静默当通过）"
    return _run(["golangci-lint", "run", "--timeout", "10m"])


def step_repolint(_argv):
    if not (_ROOT / "tools" / "repolint").is_dir():
        return None, 0.0, "tools/repolint 不存在（SKIP）"
    return _run(["go", "run", "./tools/repolint"])


def step_secrets(_argv):
    bad, seen = [], 0
    for p in _walk_sources():
        rel = _rel(p)
        if rel.startswith(SECRET_EXEMPT_PREFIX):
            continue
        try:
            if p.stat().st_size > 512 * 1024:
                continue
            for i, line in enumerate(_read_text(p).splitlines(), 1):
                seen += 1
                for name, rx in SECRET_PATTERNS:
                    if rx.search(line):
                        bad.append(f"{rel}:{i} {name}")
        except OSError:
            continue
    msg = f"扫描行数={seen} 命中={len(bad)}"
    if bad:
        return False, 0.0, msg + "\n  " + "\n  ".join(bad[:10])
    return True, 0.0, msg


def _keep_blocks(text):
    out, cur, cur_path = [], [], None

    def flush():
        if cur and cur_path and not cur_path.startswith(SECRET_EXEMPT_PREFIX):
            out.extend(cur)

    for line in text.splitlines():
        if line.startswith("@@COMMIT"):
            flush()
            cur, cur_path = [], None
            continue
        if line.startswith("diff --git "):
            flush()
            cur, cur_path = [line], None
            continue
        if line.startswith("+++ b/"):
            cur_path = line[6:].strip()
        cur.append(line)
    flush()
    return "\n".join(out)


def step_history(_argv):
    ok, _s, out = _run(["git", "log", "-p", f"-{HISTORY_N}", "--no-color",
                        "--pretty=format:@@COMMIT %h %s"], timeout=600)
    if not ok:
        return False, 0.0, f"git log 失败: {out[-200:]}"
    blob = _keep_blocks(out)
    bad = []
    for i, line in enumerate(blob.splitlines(), 1):
        for name, rx in SECRET_PATTERNS:
            if rx.search(line):
                bad.append(f"diff:{i} {name}")
    msg = f"窗口={HISTORY_N} 提交 保留行={len(blob.splitlines())} 命中={len(bad)}"
    if bad:
        return False, 0.0, msg + "\n  " + "\n  ".join(bad[:10])
    return True, 0.0, msg


def _parse_go_mod_requires(path):
    deps, in_block = set(), False
    for line in _read_text(path).splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if in_block and s and not s.startswith("//"):
            deps.add(s.split()[0])
        elif s.startswith("require ") and not s.endswith("("):
            deps.add(s.split()[1])
    return deps


def step_deps(argv):
    """go.mod 依赖面：require 清单对账——新增依赖必须显式记账（--update-deps）。"""
    cur = {}
    for mod in ("go.mod", "sdk/go/go.mod", "desktop/go.mod"):
        p = _ROOT / mod
        if p.is_file():
            cur[mod] = sorted(_parse_go_mod_requires(p))
    total = sum(len(v) for v in cur.values())
    if "--update-deps" in argv:
        _safe(DEPS_BASE).write_text(
            json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8")
        return True, 0.0, f"基线已更新（{total} 条）"
    if not DEPS_BASE.is_file():
        return None, 0.0, "无依赖基线（先 --update-deps 建立）"
    old = json.loads(_read_text(DEPS_BASE))
    added = []
    for mod, deps in cur.items():
        for d in deps:
            if d not in set(old.get(mod, [])):
                added.append(f"{mod}: +{d}")
    msg = f"require 总数={total} 新增={len(added)}"
    if added:
        return False, 0.0, msg + "\n  " + "\n  ".join(added[:12])
    return True, 0.0, msg


def step_bigfiles(_argv):
    over = []
    for p in _walk_sources():
        rel = _rel(p)
        try:
            size = p.stat().st_size
            if size > MAX_FILE_BYTES:
                over.append(f"{rel} {size // 1024}KB > {MAX_FILE_BYTES // 1024}KB")
                continue
            n = len(_read_text(p).splitlines())
            if n > MAX_FILE_LINES:
                over.append(f"{rel} {n} 行 > {MAX_FILE_LINES}")
        except OSError:
            continue
    over.sort()
    msg = f"上限={MAX_FILE_LINES} 行 / {MAX_FILE_BYTES // 1024}KB 超限={len(over)}"
    if over:
        return False, 0.0, msg + "\n  " + "\n  ".join(over[:10])
    return True, 0.0, msg


def step_freshness(argv):
    if not LEDGER.is_file():
        return None, 0.0, "无审计账本 .audit-ledger.json（副本审计仪式后记账）"
    entries = (json.loads(_read_text(LEDGER)) or {}).get("entries") or []
    if not entries:
        return False, 0.0, "账本为空"
    last = entries[-1]
    age = (date.today() - date.fromisoformat(last["date"])).days
    behind = None
    if last.get("head"):
        ok, _s, out = _run(["git", "rev-list", "--count",
                            f"{last['head']}..HEAD"], timeout=120)
        behind = int(out.strip()) if ok and out.strip().isdigit() else None
    stale = age > AUDIT_MAX_DAYS or (behind is not None and behind > AUDIT_MAX_COMMITS)
    msg = (f"最新={last.get('round')}@{last['date']} age={age}d "
           f"behind={behind} (≤{AUDIT_MAX_DAYS}d/≤{AUDIT_MAX_COMMITS})")
    if stale and "--allow-stale" not in argv:
        return False, 0.0, msg + " STALE——补做副本审计或 --allow-stale"
    return True, 0.0, msg


def step_test(_argv):
    return _run(["go", "test", "./...", "-count=1"], timeout=7200)


STEPS = [
    ("repolint", step_repolint, "fast", "仓内 repolint"),
    ("vet", step_vet, "fast", "go vet ./..."),
    ("golangci", step_golangci, "fast", "golangci-lint run"),
    ("secrets", step_secrets, "fast", "工作树明文红线"),
    ("history", step_history, "fast", "近 50 提交 diff 明文"),
    ("deps", step_deps, "fast", "依赖清单对账"),
    ("bigfiles", step_bigfiles, "fast", "大文件/上帝文件上限"),
    ("freshness", step_freshness, "fast", "审计时效"),
    ("test", step_test, "full", "go test ./...（慢档）"),
]


def main(argv):
    if "--list" in argv:
        for n, _f, tier, why in STEPS:
            print(f"{tier:4s} {n:10s} {why}")
        return 0
    fast = "--fast" in argv
    only = None
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            only = {s.strip() for s in argv[i + 1].split(",")}
    failed, ran = [], 0
    for name, fn, tier, why in STEPS:
        if fast and tier != "fast":
            continue
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            ok, _s, out = fn(argv)
        except Exception as ex:                       # 步骤炸=红，不吞
            ok, out = False, f"{type(ex).__name__}: {ex}"
        secs = time.time() - t0
        tag = "SKIP" if ok is None else ("OK  " if ok else "FAIL")
        print(f"{tag} {name:10s} {secs:6.1f}s {why}")
        if out:
            for ln in (out.splitlines() or [""])[:20]:   # 逐行打印：不触碰替换面
                print("  " + ln[:200])
        ran += 1
        if ok is False:
            failed.append(name)
    print(f"LOCAL-AUDIT {'OK' if not failed else 'FAIL'} steps={ran} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
