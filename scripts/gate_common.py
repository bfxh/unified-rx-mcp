r"""三道结构门的共用件：**扫描面 / 基线读写 / 棘轮判定 / 报告**只留一份实现。

为什么抽出来：stdout / nest / args 三道门判据各不相同，但「扫哪些文件、基线怎么读写、
涨了怎么判、结果怎么报」完全一样。各写一遍就会有 N 份各差一点的副本——**判据口径一漂移，
同一个文件在不同门里算出来的数不一样，比没有门更糟**；三份近似文件还会被 dupe 门判雷同。

本文件**不是一道门**（不进 `local_gate.py` 的 STEPS、不出现在 workflow 里），只被各门 import。
"""
import argparse
import ast
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 目录剪枝（与 god_gate 的 exclude 同口径）
EXCLUDE_DIRS = {".git", "target", "node_modules", "__pycache__", "dist", "build", ".venv"}
# 生成物 / 语料 / 文档 / 测试面：不是产品面，不进结构门的口径
SKIP_PREFIX = ("bench/", "tests/", "spec/", "docs/", "rust/", "plugins/", "skills/")


def py_files():
    """产品面 `.py` 文件（相对路径，正斜杠）。

    刻意**不接 root 形参**：把目录当参数传进 `os.walk` 会被 taint 门判成"外部输入流入
    遍历原语"（本仓 data-flow 门的口径）。这三道门本来就只扫本仓工作树，用模块级
    `ROOT` 常量即可——顺带少一个能被人拿去扫别处的入口。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = (pathlib.Path(dirpath) / fn).relative_to(ROOT).as_posix()
            if rel.startswith(SKIP_PREFIX):
                continue
            out.append(rel)
    return sorted(out)


def parse(rel):
    """解析成 AST；语法错误的文件跳过（不是本门的职责，别让门自己崩）。"""
    try:
        return ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return None


def iter_funcs(tree):
    """所有（含嵌套的）函数定义。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def load_baseline(path):
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"基线 {path.name} 不是合法 JSON（{e}）——若上次写入被中断，重跑 --write 即可")


def write_baseline(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)          # 原子替换：中断也不留半截基线


def ratchet(cur, base, label, bad, shrank):
    """只准减：新增即红，减少则提示可收紧。"""
    for rel, v in sorted(cur.items()):
        bv = base.get(rel, 0)
        if v > bv:
            bad.append(f"{label} {rel}: {bv} → {v}（新增 {v - bv} 处，只准减）")
        elif v < bv:
            shrank.append(f"{label} {rel}: {bv} → {v}")


def report(name, bad, shrank, limit=25):
    for line in bad[:limit]:
        print("  ✗", line)
    if len(bad) > limit:
        print(f"  …另有 {len(bad) - limit} 条")
    if shrank:
        print(f"  （{len(shrank)} 条可收紧，跑 --write-baseline 更新）")
    print(f"{name} {'FAIL' if bad else 'OK'} 命中={len(bad)}")
    return 1 if bad else 0


def add_args(ap, default_top=12):
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=default_top)
    return ap


def run_gate(name, baseline_rel, scan, label, argv=None):
    """三道门共用的 main 流程：扫描 → 写基线 / 棘轮比对 → 报告。"""
    ap = add_args(argparse.ArgumentParser())
    a = ap.parse_args(argv)
    cur = scan()
    total = sum(cur.values())
    print(f"{name} 文件={len(cur)} 命中={total}")
    if a.list:
        for r, n in sorted(cur.items(), key=lambda kv: -kv[1])[:a.top]:
            print(f"  {n:3d}  {r}")
        return 0
    bpath = ROOT / baseline_rel
    if a.write_baseline:
        write_baseline(bpath, cur)
        print(f"已写基线 {baseline_rel}（{total} {label}）——此后只准减")
        return 0
    base = load_baseline(bpath)
    if not base:
        print("警告：无基线 ⇒ 不判；跑 --write-baseline 才会管住存量")
    bad, shrank = [], []
    ratchet(cur, base, label, bad, shrank)
    return report(name, bad, shrank)
