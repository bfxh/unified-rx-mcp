r"""stdout 协议门：MCP 服务面（根 `*.py` + `tools/`）里的 `print(`。

**为什么这道门在本仓特别要紧**：MCP 走 **JSON-RPC over stdio**——stdout 就是协议通道。
工具实现里任何一句 `print` 都会把非协议字节写进这条流，client 端轻则解析失败重连，
重则把调试输出当成工具结果。**这类 bug 的诡异之处在于：手动跑永远正常**（那时没人监听
stdout），只有接进真 client 才炸，所以测试抓不到、人也想不起来。正解是 `logging` 写 stderr，
或把信息放进结构化回包。

扫描面与豁免（都写理由，不是"报多了就关掉"）：
  · 不扫 `scripts/` `bench/` `tests/` `spec/` `docs/` `rust/` `plugins/` `skills/`——
    门脚本与命令行工具**打印到 stdout 就是本分**（人要读报告）；
  · 豁免 `server.py` 的 `selftest()`——那是 `python server.py --selftest` 的子命令，
    走的是 CLI 模式不是 stdio 会话，9 处打印都是给人看的诊断行。

判据：其余 MCP 服务面出现 `print(` ⇒ 棘轮（存量入基线，只准减）。

用法：
  python -X utf8 scripts/stdout_gate.py                 # 判红
  python -X utf8 scripts/stdout_gate.py --list          # 看存量分布
  python -X utf8 scripts/stdout_gate.py --write-baseline
"""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gate_common as gc

NAME = "STDOUT-GATE"
BASELINE = "stdout-baseline.json"
# 命令行自检子命令：CLI 模式下打印给人看，不是 stdio 会话里的工具输出
EXEMPT_FUNC = {"selftest"}


def count_prints(node):
    return sum(1 for c in ast.walk(node)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and c.func.id == "print")


def scan():
    cur = {}
    for rel in gc.py_files():
        # MCP 服务面 = 根 *.py + tools/；scripts/ 等命令行工具面不判（见文件头）
        if rel.startswith("scripts/") or rel == "conftest.py":
            continue
        if "/" in rel and not rel.startswith("tools/"):
            continue
        tree = gc.parse(rel)
        if tree is None:
            continue
        n = 0
        for fn in gc.iter_funcs(tree):
            if fn.name in EXEMPT_FUNC:
                continue
            n += count_prints(fn)
        if n:
            cur[rel] = n
    return cur


def main(argv=None) -> int:
    return gc.run_gate(NAME, BASELINE, scan, "处 print", argv)


if __name__ == "__main__":
    sys.exit(main())
