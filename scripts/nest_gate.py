r"""嵌套深度门：函数里控制流的**嵌套层数**（箭头代码 / Arrow Code）。

与已有的 `C901`（ruff 的 mccabe 复杂度）是**两维**：复杂度数的是「分支多少」，这一门数的是
「分支套了几层」。同样是 10 个分支，写成卫语句平铺与写成五层 `if` 套 `with` 套 `try`，
复杂度可能一模一样，但后者改一处要同时记住五层上下文——review 时人眼配平不过来。
本仓 `ruff.toml` 明确不收 PL 子集（复杂度另由 god 门管），而 god 门只管**函数长度**，
嵌套深度这一维此前没人管。

判据（`ast` 遍历，不是正则 ⇒ 不受注释/字符串干扰）：
  层数从函数体起算，每进一层 `if` / `for` / `while` / `with` / `try`（含 async 变体）+1。
  · 深度 > 4 —— 棘轮（只准减）
  · 深度 > 8 —— 棘轮（本仓最深 9，见 `tools/scip.py`）

用法：
  python -X utf8 scripts/nest_gate.py                 # 判红
  python -X utf8 scripts/nest_gate.py --list          # 看分布
  python -X utf8 scripts/nest_gate.py --write-baseline
"""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gate_common as gc

NAME = "NEST-GATE"
BASELINE = "nest-baseline.json"
LIM = 4
LIM_DEEP = 8
BLOCK = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)


def depth(node, d=0):
    best = d
    for child in ast.iter_child_nodes(node):
        best = max(best, depth(child, d + (1 if isinstance(child, BLOCK) else 0)))
    return best


def scan():
    over, deep = {}, {}
    for rel in gc.py_files():
        tree = gc.parse(rel)
        if tree is None:
            continue
        n4 = n8 = 0
        for fn in gc.iter_funcs(tree):
            d = depth(fn)
            if d > LIM:
                n4 += 1
            if d > LIM_DEEP:
                n8 += 1
        if n4:
            over[rel] = n4
        if n8:
            deep[rel] = n8
    return {"over4": over, "over8": deep}


def main(argv=None) -> int:
    import argparse
    ap = gc.add_args(argparse.ArgumentParser())
    a = ap.parse_args(argv)
    cur = scan()
    summary = (f"over4={sum(cur['over4'].values())} over8={sum(cur['over8'].values())}")
    print(f"{NAME} {summary}")
    if a.list:
        for key in ("over4", "over8"):
            for r, n in sorted(cur[key].items(), key=lambda kv: -kv[1])[:a.top]:
                print(f"  {key:6s} {n:3d}  {r}")
        return 0
    bpath = gc.ROOT / BASELINE
    if a.write_baseline:
        gc.write_baseline(bpath, cur)
        print(f"已写基线 {BASELINE}（{summary}）——此后只准减")
        return 0
    base = gc.load_baseline(bpath)
    if not base:
        print("警告：无基线 ⇒ 不判；跑 --write-baseline 才会管住存量")
    bad: list = []          # mypy 默认档：空列表要显式注解（类型门零容忍）
    shrank: list = []
    gc.ratchet(cur["over4"], base.get("over4", {}), "nest>4", bad, shrank)
    gc.ratchet(cur["over8"], base.get("over8", {}), "nest>8", bad, shrank)
    return gc.report(NAME, bad, shrank)


if __name__ == "__main__":
    sys.exit(main())
