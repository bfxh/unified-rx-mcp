# -*- coding: utf-8 -*-
"""任务级工具评测（S144，兑现 EXTERNAL-ALIGNMENT B2）。

外部方法论（Anthropic writing-tools-for-agents）：工具面要用**真实多步任务**评测，
指标 = 调用数 / 错误数 / 返回体量（token 代理）——而不是"工具能不能跑"。

本仓实现为**确定性任务重放**：每任务自建夹具（TEMP 内）、走 registry.call 打
既定最小调用序列、以**结果断言**判成败，并记账（calls/errors/chars）。它测的是
"工具面本身的可用性与代价"——同一任务序列跨轮次对比，能抓：工具行为回归、
响应体膨胀（描述/字段改动）、契约漂移。

用法：
  python -X utf8 bench/tool_evals.py                 # 跑一遍，打印表
  python -X utf8 bench/tool_evals.py --check         # 对照 spec/tool-evals-baseline.json
  python -X utf8 bench/tool_evals.py --update-baseline
  UNIFIED_RX_EVAL_SABOTAGE=1 …                       # 自检：任务 1 必红（真门验证）

--check 判红条件：①任一任务失败；②任一任务返回体量 > 基线 ×1.10（膨胀）。
沙盒：自给自足声明 UNIFIED_RX_SANDBOX=夹具根（同 ci_secrets_gate 纪律）。

写盘纪律（Mimosa 两次预警后照录）：全部经 `_p()` 校验（resolve 后必须在夹具根
内）；夹具不写危险 API 字面量（坏代码用无害规则 except_pass 植入）；载荷用
sha256 哈希流（确定性 + 无弱随机）。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

W = Path(tempfile.mkdtemp(prefix="urx-tool-evals-")).resolve()
os.environ["UNIFIED_RX_SANDBOX"] = str(W)     # 自给自足（先声明再 import）
# 确定性（CI 首跑实锤）：CI 装了 pylsp → ide_impact 会走 LSP 档（冷启动 ~19s、
# 结果随环境变），本地没装则走名字解析档。评测要跨机可比 → 命令指向不存在程序，
# 令 LSP 档确定性不可用（工具语义：能力缺席如实降级，这条正是被评测的行为）。
os.environ["UNIFIED_RX_LSP_CMD_PYTHON"] = "urx-nonexistent-lsp"
os.environ["UNIFIED_RX_LSP_CMD_RUST"] = "urx-nonexistent-lsp"

import registry  # noqa: E402
import tools  # noqa: E402,F401

BASELINE = ROOT / "spec" / "tool-evals-baseline.json"
SABOTAGE = os.environ.get("UNIFIED_RX_EVAL_SABOTAGE") == "1"


def _p(rel):
    """夹具路径：resolve 后必须落在 W 内（写前显式校验，防路径穿越）。"""
    q = (W / rel).resolve()
    if W not in q.parents and q != W:
        raise ValueError(f"夹具路径越界: {rel}")
    return q


def _w(rel, text):
    q = _p(rel)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(text, encoding="utf-8")
    return str(q)


def _blob(n, seed=b"urx-evals"):
    """确定性载荷（sha256 哈希流；不用 random——弱随机既非必要也过不了门）。"""
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(seed + i.to_bytes(4, "little")).digest()
        i += 1
    return out[:n]


class Rec:
    """记账器：包住 registry.call，统计 calls/errors/chars。"""

    def __init__(self):
        self.calls = 0
        self.errors = 0
        self.chars = 0

    def call(self, tool, args):
        self.calls += 1
        r = registry.call(tool, args)
        if not r.get("ok"):
            self.errors += 1
        self.chars += len(json.dumps(r, ensure_ascii=False))
        return r


def _get(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# ---- 任务定义：fn(rec) -> (ok, detail) ----

def t_fs_roundtrip(rec):
    p = _w("rt/a.txt", "hello-evals\n")
    rec.call("fs_write", {"path": p, "__authorized": True, "content": "hello-evals\n"})
    r = rec.call("fs_read", {"path": p})
    got = _get(r, "result", "content")
    want = "SABOTAGED" if SABOTAGE else "hello-evals"
    return (r.get("ok") and isinstance(got, str) and want in got), "读回内容一致"


def t_bug_scan_except_pass(rec):
    # 无害规则植入（except_pass）——夹具不携带危险 API 字面量（夹具纪律）
    bad = ("def f(x):\n"
           "    try:\n"
           "        return int(x)\n"
           "    except Exception:\n"
           "        pass\n")
    p = _w("bs/bad.py", bad)
    r = rec.call("bug_scan", {"path": str(Path(p).parent)})
    fs = _get(r, "result", "issues") or _get(r, "result", "findings") or []
    blob = json.dumps(fs, ensure_ascii=False)
    return (r.get("ok") and "except_pass" in blob), f"issues={len(fs)}"


def t_secrets_aws(rec):
    fake = "AKIA" + "IOSFODNN7EXAMPLE"      # 运行时拼接（明文不进仓）
    _w("sec/leak.py", f'KEY = "{fake}"\n')
    r = rec.call("secrets_hunt", {"path": str(_p("sec"))})
    hits = _get(r, "result", "hits") or []
    return (r.get("ok") and len(hits) >= 1), f"hits={len(hits)}"


def t_taint_open(rec):
    _w("tt/t.py", "def read_it(p):\n"
                  "    return open(p, encoding='utf-8').read()\n")
    r = rec.call("rust_taint_scan", {"root": str(_p("tt"))})
    res = r.get("result") or {}
    hits = res.get("findings") or res.get("definite") or []
    n = len(hits) if isinstance(hits, list) else int(hits or 0)
    return (r.get("ok") and n >= 1), f"findings={n}"


def t_dead_code(rec):
    _w("dc/m.py", "def used():\n    return 1\n\n\n"
                  "def unused_helper():\n    return 2\n\n\n"
                  "used()\n")
    r = rec.call("ide_dead_code", {"path": str(_p("dc"))})
    dead = _get(r, "result", "dead") or []
    blob = json.dumps(dead, ensure_ascii=False)
    return (r.get("ok") and "unused_helper" in blob), f"dead={len(dead)}"


def t_callgraph(rec):
    _w("cg/a.py", "def target_fn(x):\n    return x + 1\n")
    _w("cg/b.py", "from a import target_fn\n\n\n"
                  "def caller():\n    return target_fn(1)\n")
    r = rec.call("ide_callgraph", {"root": str(_p("cg")),
                                   "symbol": "target_fn", "direction": "callers"})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False)
    return (r.get("ok") and "b.py" in blob), "callers 含 b.py"


def t_impact(rec):
    a = _w("im/a.py", "def target_fn(x):\n    return x + 1\n")
    _w("im/b.py", "from a import target_fn\n\n\n"
                  "def caller():\n    return target_fn(1)\n")
    r = rec.call("ide_impact", {"file": a, "line": 0})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False)
    return (r.get("ok") and "b.py" in blob), "影响面含 b.py"


def t_edit_and_test(rec):
    _w("et/code.py", "def add(a, b):\n    return a + b\n")
    t = _w("et/test_code.py", "from code import add\n\n\n"
                              "def test_add():\n    assert add(1, 2) == 4\n")
    e = rec.call("ide_edit_multi", {
        "file_path": t,
        "edits": [{"old_lines": ["    assert add(1, 2) == 4"],
                   "new_lines": ["    assert add(1, 2) == 3"]}],
        "__authorized": True})
    if not e.get("ok"):
        return False, f"edit failed: {e.get('error')}"
    r = rec.call("ide_test", {"path": str(_p("et")), "__authorized": True})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False).lower()
    ok = r.get("ok") and ("passed" in blob or '"failed": 0' in blob)
    return bool(ok), f"test ok={r.get('ok')}"


def t_coverage(rec):
    _w("cv/cov.py", "def f(x):\n    if x:\n        return 1\n    return 0\n")
    d = _w("cv/driver.py", "import cov\n\nprint(cov.f(1))\n")
    r = rec.call("code_coverage", {"script": d, "source_dir": str(_p("cv")),
                                   "__authorized": True})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False)
    return (r.get("ok") and ("cov.py" in blob or "missing" in blob)), "覆盖率报告产出"


def t_near_dupes(rec):
    base = ("def alpha(x):\n    y = x + 1\n    z = y * 2\n    return z\n\n"
            "def beta(a, b):\n    return a - b\n\n"
            "CONST = 42\n")
    _w("nd/one.py", base)
    _w("nd/two.py", base.replace("alpha", "alpha2"))
    r = rec.call("near_dupes", {"path": str(_p("nd"))})
    res = r.get("result") or {}
    blob = json.dumps(res, ensure_ascii=False)
    n = len(res.get("clusters") or res.get("pairs") or res.get("groups") or [])
    return (r.get("ok") and (("one.py" in blob and "two.py" in blob) or n >= 1)), \
        f"clusters={n}"


def t_dep_cycle(rec):
    _w("dp/x.py", "import y\n\n\ndef fx():\n    return y.fy()\n")
    _w("dp/y.py", "import x\n\n\ndef fy():\n    return x.fx()\n")
    r = rec.call("dep_graph", {"path": str(_p("dp"))})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False).lower()
    return (r.get("ok") and "cycle" in blob), "环检出"


def t_xor_file(rec):
    key = 0x5A
    crib = b"MZ\x90\x00\x03\x00\x00\x00"
    payload = bytes(b ^ key for b in (crib + _blob(4096)))
    q = _p("xf/obf.bin")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_bytes(payload)
    r = rec.call("file_scan", {"path": str(_p("xf")),
                               "xor_crib": "hex:4d5a900003000000"})
    fs = _get(r, "result", "findings") or []
    keys = (fs[0].get("xor_keys") if fs and isinstance(fs[0], dict) else None) or []
    hit = any(isinstance(k, dict) and k.get("key") == key for k in keys)
    return (r.get("ok") and hit), f"xor_keys={keys[:2]}"


def t_repo_map(rec):
    _w("rm/m.py", "def solo():\n    return 1\n")
    r = rec.call("repo_map", {"root": str(_p("rm")), "focus": ["m.py"],
                              "budget_tokens": 512})
    blob = json.dumps(r.get("result") or {}, ensure_ascii=False)
    return (r.get("ok") and "solo" in blob), "符号地图含 solo"


TASKS = [
    ("fs_roundtrip", "写读往返（授权→读回一致）", t_fs_roundtrip),
    ("bug_scan_except_pass", "静态扫描找到植入的 except_pass", t_bug_scan_except_pass),
    ("secrets_aws", "密钥扫描找到植入的假 AWS key", t_secrets_aws),
    ("taint_open", "污点扫描找到 参数→open 数据流", t_taint_open),
    ("dead_code", "死代码扫描找到未引用函数", t_dead_code),
    ("callgraph", "调用图给出 target_fn 的调用者", t_callgraph),
    ("impact", "影响面含调用文件", t_impact),
    ("edit_and_test", "改代码→跑测试 由红转绿", t_edit_and_test),
    ("coverage", "覆盖率测量产出报告", t_coverage),
    ("near_dupes", "近似重复两文件被聚类", t_near_dupes),
    ("dep_cycle", "依赖环检出", t_dep_cycle),
    ("xor_file", "文件扫描锁定异或密钥", t_xor_file),
    ("repo_map", "符号地图含 focus 定义", t_repo_map),
]


def run_all():
    rows = []
    for name, why, fn in TASKS:
        rec = Rec()
        t0 = time.time()
        try:
            ok, detail = fn(rec)
        except Exception as ex:                      # 任务炸=任务红（不吞）
            ok, detail = False, f"{type(ex).__name__}: {ex}"
        rows.append({"name": name, "why": why, "ok": bool(ok), "detail": detail,
                     "calls": rec.calls, "errors": rec.errors,
                     "chars": rec.chars, "ms": int((time.time() - t0) * 1000)})
    return rows


def main(argv):
    rows = run_all()
    upd = "--update-baseline" in argv
    chk = "--check" in argv or upd
    base = {}
    if chk and BASELINE.is_file():
        base = (json.loads(BASELINE.read_text(encoding="utf-8")) or {}).get("tasks", {})
    total_calls = sum(r["calls"] for r in rows)
    total_chars = sum(r["chars"] for r in rows)
    total_err = sum(r["errors"] for r in rows)
    for r in rows:
        b = base.get(r["name"])
        flag = ""
        if chk and b:
            if r["chars"] > b.get("chars", 0) * 1.10:
                flag = " BLOAT"
            if not r["ok"] and b.get("ok"):
                flag = " REGRESS"
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['name']:22s} calls={r['calls']} "
              f"err={r['errors']} chars={r['chars']} {r['ms']}ms {r['detail']}{flag}")
    print(f"TOTAL tasks={len(rows)} calls={total_calls} errors={total_err} "
          f"chars={total_chars}")
    if upd:
        BASELINE.write_text(json.dumps(
            {"note": "S144 任务级 evals 基线（--update-baseline 生成；check 判红="
                     "任务失败或 chars>基线×1.10）",
             "tasks": {r["name"]: {"ok": r["ok"], "calls": r["calls"],
                                   "chars": r["chars"]} for r in rows}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print("BASELINE 已更新:", BASELINE)
    failed = [r["name"] for r in rows if not r["ok"]]
    bloat = [r["name"] for r in rows
             if chk and base.get(r["name"])
             and r["chars"] > base[r["name"]]["chars"] * 1.10]
    if failed or bloat:
        sys.exit(f"TOOL-EVALS FAIL: failed={failed} bloat={bloat}")
    print("TOOL-EVALS OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        if "--keep" not in sys.argv and not SABOTAGE:
            shutil.rmtree(W, ignore_errors=True)
