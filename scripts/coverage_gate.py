"""scripts/coverage_gate.py —— Rust 覆盖率门（S172）：llvm-cov 行覆盖率**只准升**棘轮。

判据：`cargo llvm-cov --json --summary-only` 的 `totals.line_percent` ≥ 基线
（`spec/coverage-baseline.json`）；高于基线提示收紧（`--write-baseline`）。无基线 ⇒
PASS + 打印实测值（**只观测**——首次启用由 CI 首跑取数，再入册棘轮）。

**为什么本地默认不测**（实测 2026-09-25）：覆盖率编译要 profiler runtime——本机
windows-gnu 工具链**没有**（`can't find crate for profiler_builtins`），msvc 工具链缺
**link.exe**（没装 VS Build Tools）；CI 的 windows runner 自带 MSVC ⇒ 测量在 CI。
本地用 `UNIFIED_RX_COV_TOOLCHAIN` 指定带 link 能力的工具链也能测（如
`+stable-x86_64-pc-windows-msvc`，装了 VS Build Tools 的机器）。

工具/测量失败 **FAIL 不静默**（本地门链里归 coverage 档，默认显式 SKIP，同 timing 形态）。

用法：
  python -X utf8 scripts/coverage_gate.py                  # 判红
  python -X utf8 scripts/coverage_gate.py --write-baseline # 记录/收紧基线（只准升）
  UNIFIED_RX_COV_TOOLCHAIN=+stable-x86_64-pc-windows-msvc python -X utf8 scripts/coverage_gate.py
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess

BASELINE = pathlib.Path(__file__).resolve().parent.parent / "spec" / "coverage-baseline.json"


def measure() -> float:
    """跑 llvm-cov 返回 line_percent；任何失败上抛 RuntimeError（不静默）。"""
    if shutil.which("cargo") is None:
        raise RuntimeError("cargo 不可用——覆盖率门需要 Rust 工具链（CI 自带）")
    tc = os.environ.get("UNIFIED_RX_COV_TOOLCHAIN", "")
    cmd = ["cargo"] + ([tc] if tc else []) + [
        "llvm-cov", "--manifest-path",
        str(pathlib.Path(__file__).resolve().parent.parent / "rust" / "Cargo.toml"),
        "--json", "--summary-only"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", shell=False, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f"llvm-cov 测量失败 rc={p.returncode}："
                           f"{(p.stderr or '')[-300:]}")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"llvm-cov 输出不是 JSON（{e}）：{p.stdout[:200]}") from e
    lp = _extract_line_percent(data)
    if lp is None:
        raise RuntimeError(f"llvm-cov 输出缺行覆盖率字段（形状见下）：{p.stdout[:200]}")
    return float(lp)


def _extract_line_percent(data):
    """兼容两种输出形状：① llvm-cov export（`data[0].totals.lines.percent`，CI 实测）；
    ② 扁平 `totals.line_percent`。找不到返回 None。"""
    totals = data.get("totals") if isinstance(data, dict) else None
    if isinstance(totals, dict):
        lp = totals.get("line_percent")
        if isinstance(lp, (int, float)):
            return lp
        lines = totals.get("lines")
        if isinstance(lines, dict) and isinstance(lines.get("percent"), (int, float)):
            return lines["percent"]
    for chunk in (data.get("data") or []) if isinstance(data, dict) else []:
        t = chunk.get("totals") or {}
        lines = t.get("lines") or {}
        if isinstance(lines.get("percent"), (int, float)):
            return lines["percent"]
    return None


def load_baseline() -> float | None:
    if not BASELINE.is_file():
        return None
    try:
        return float(json.loads(BASELINE.read_text(encoding="utf-8"))["line_percent"])
    except (OSError, ValueError, KeyError) as e:
        raise RuntimeError(f"基线文件坏了（{BASELINE}）：{e}") from e


def evaluate(value: float, baseline: float | None) -> tuple[bool, str]:
    """(是否通过, 说明)。纯函数——金丝雀直接测它。只准升：低于基线即红。

    **容差 ±0.005 个百分点**（半个显示位）：CI 两次实测 67.87% 基线判红——
    实测值是 67.8651…，显示四舍五入成 67.87 而比较用的是原值 ⇒ 浮点尘被当下降
    （CI 实锤）。棘轮要拦的是真实下降（≥0.01 个百分点），不是舍入噪声。
    """
    if baseline is None:
        return True, f"尚无基线（实测 {value:.2f}%，只观测；--write-baseline 入册后棘轮生效）"
    if value < baseline - 0.005:
        return False, f"覆盖率 {value:.2f}% < 基线 {baseline:.2f}%（只准升）"
    if value > baseline + 0.005:
        return True, (f"覆盖率 {value:.2f}% > 基线 {baseline:.2f}%"
                      f"（可 --write-baseline 收紧）")
    return True, f"覆盖率 {value:.2f}% ≈ 基线 {baseline:.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true")
    a = ap.parse_args()
    try:
        value = measure()
        baseline = load_baseline()
    except RuntimeError as e:
        print(f"FAIL coverage-gate {e}")
        return 1
    if a.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"_doc": "Rust 行覆盖率基线（scripts/coverage_gate.py）：只准升，不许降。",
             "line_percent": round(value, 2)}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"COVERAGE-GATE 已写基线 {value:.2f}%")
        return 0
    ok, why = evaluate(value, baseline)
    print(f"COVERAGE-GATE line={value:.2f}% 基线="
          f"{'未记' if baseline is None else f'{baseline:.2f}%'} ⇒ {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
