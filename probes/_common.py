"""probes 公共框架：直接调用 unified-rx server 内部函数做契约验证。

用法：
    python probes/_common.py --probe <name>   # 单跑
    python probes/run_all.py                  # 全跑（退出码 0=全过）

每条 probe 是独立函数，通过 stdio 或直接 import server 模块调用
（本仓库是 unified-rx 开发版，可直接 import）。
"""
import json
import os
import sys
import traceback

# 仓库根（probes/ 的父目录）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

RESULTS = []


def probe(name: str):
    """装饰器：注册探针。函数返回 (ok: bool, detail: str)。"""
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def run_probe(name: str, fn):
    """执行单个探针，返回结构化结果。"""
    try:
        ok, detail = fn()
        return {"name": name, "ok": bool(ok), "detail": detail,
                "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": "",
                "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"}


def run_all():
    """跑全部探针，返回汇总。"""
    out = []
    for name, fn in RESULTS:
        out.append(run_probe(name, fn))
    return out


def main():
    """CLI 入口：python probes/_common.py [--probe NAME] [--json]"""
    argv = sys.argv[1:]
    only = None
    as_json = "--json" in argv
    if "--probe" in argv:
        only = argv[argv.index("--probe") + 1]
    if only:
        hits = [(n, f) for n, f in RESULTS if n == only]
        if not hits:
            print(f"probe not found: {only}")
            sys.exit(2)
        res = [run_probe(hits[0][0], hits[0][1])]
    else:
        res = run_all()
    ok_count = sum(1 for r in res if r["ok"])
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for r in res:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"[{mark}] {r['name']}: {r['detail']}")
            if r["error"]:
                print(f"       {r['error']}")
        print(f"\n{ok_count}/{len(res)} probes passed")
    sys.exit(0 if ok_count == len(res) else 1)


if __name__ == "__main__":
    main()
