"""scripts/type_gate.py —— Python 类型门（S171）：mypy 默认档**零容忍**。

判据：产品面（默认 `registry.py server.py toolmeta.py tools/ scripts/`）在 mypy 默认档下
**0 error**——2026-09-24 实测已清零（19 → 0），所以**不设基线棘轮**：冒一条即红。

**范围**：`tests/`、`bench/` 暂不入册（夹具与跑批脚本，噪声面不同，另行评估）；
`--paths` 可自定义目标（金丝雀就这么用）。
**工具缺失不静默**：mypy 不在当前解释器里直接 FAIL（CI 由 `ci-requirements.txt` 钉版本装）。

为什么与 ruff 门并存：ruff 的 F/E/B 管语法与常见错，mypy 管**类型面**——不是一类。
首次启用就抓到 1 处变量复用（`Match[str]` 被赋成 `Optional`）+ 16 处模块级空容器缺注解。

用法：
  python -X utf8 scripts/type_gate.py                     # 判红（默认 root=.）
  python -X utf8 scripts/type_gate.py --list              # 逐条打印诊断
  python -X utf8 scripts/type_gate.py --root <r> --paths bad.py
"""
import argparse
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

DEFAULT_PATHS = ("registry.py", "server.py", "toolmeta.py", "tools", "scripts")
_DIAG = re.compile(r": error: ")


def run_mypy(root: pathlib.Path, paths: list[str]) -> tuple[int, list[str]]:
    """跑 mypy（默认档、不增量），返回 (退出码, 诊断行)。"""
    if importlib.util.find_spec("mypy") is None:
        raise RuntimeError(
            "mypy 不可用——本地 `python -m pip install mypy==2.3.1`，"
            "CI 由 .github/ci-requirements.txt 安装（缺工具不静默降级）")
    cache = os.path.join(os.environ.get("TEMP", "/tmp"), "urx-mypy-gate")
    p = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary",
                        "--no-incremental", "--cache-dir", cache, *paths],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(root))
    out = (p.stdout or "") + (p.stderr or "")
    if "No module named mypy" in out:
        raise RuntimeError("mypy 不可用（解释器里没有该模块）")
    return p.returncode, [ln for ln in out.splitlines() if _DIAG.search(ln)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--paths", nargs="*", default=list(DEFAULT_PATHS))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    paths = [p for p in a.paths if (root / p).exists()]
    if not paths:
        print(f"FAIL type-gate 目标一个都不存在（root={root}）")
        return 1

    try:
        rc, diags = run_mypy(root, paths)
    except RuntimeError as e:
        print(f"FAIL type-gate {e}")
        return 1

    print(f"TYPE-GATE root={root} 目标={len(paths)} 诊断={len(diags)}（mypy 默认档，零容忍）")
    if a.list or diags:
        for ln in diags[:40]:
            print(f"  ✗ {ln}")
        if len(diags) > 40:
            print(f"  …（共 {len(diags)} 条，只打前 40）")
    if diags or rc not in (0, 1):
        print(f"TYPE-GATE FAIL 诊断={len(diags)} rc={rc}")
        return 1
    print("TYPE-GATE OK 0 error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
