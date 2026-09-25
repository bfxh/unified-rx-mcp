r"""参数个数门：函数形参超过阈值（默认 >7）。

参数一多，调用方就得记住每个位置是什么；两个相邻的 `bool` / `str` 参数随时能被对调，
而**编译器一声不吭**——这是最典型的一类「编译通过但语义错了」的 bug。正解是把一组参数
收进一个 dataclass / 配置对象。

为什么本仓此前没管：`ruff.toml` 明确不收 PL 子集（`PLR0913` 正是这条），理由写在配置里
（"PL 全是本仓的自觉选择"）。那是**有意**关掉的，但关掉不等于这条维度没有价值——
所以这里用独立的一门补上，按棘轮走，不进 ruff 的规则集（免得和既有取舍打架）。

判据（`ast` 精确计数，不是正则）：
  形参数 = posonly + 普通 + kwonly（`self` / `cls` 计入，与方法同口径——否则「把方法改成
  自由函数」就能绕过去）；`*args` / `**kwargs` 不计（它们不改变调用方要记的位置数）。
  > 7 —— 棘轮（只准减）。

用法：
  python -X utf8 scripts/args_gate.py                 # 判红
  python -X utf8 scripts/args_gate.py --list          # 看分布
  python -X utf8 scripts/args_gate.py --write-baseline
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gate_common as gc

NAME = "ARGS-GATE"
BASELINE = "args-baseline.json"
MAX_ARGS = 7


def nparams(fn):
    a = fn.args
    return len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)


def scan():
    cur = {}
    for rel in gc.py_files():
        tree = gc.parse(rel)
        if tree is None:
            continue
        n = sum(1 for fn in gc.iter_funcs(tree) if nparams(fn) > MAX_ARGS)
        if n:
            cur[rel] = n
    return cur


def main(argv=None) -> int:
    return gc.run_gate(NAME, BASELINE, scan, f"个 >{MAX_ARGS} 参函数", argv)


if __name__ == "__main__":
    sys.exit(main())
