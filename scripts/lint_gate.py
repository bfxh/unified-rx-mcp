"""scripts/lint_gate.py —— Python 静态门（S171）：ruff **逐规则**棘轮。

判据：按规则号统计 `ruff check`（配置来自仓内 `ruff.toml`）的命中数，记进
`lint-baseline.json`；此后**任何规则涨**、或**出现新规则号**即红（退出码 1）。
低于基线只提示（`--write-baseline` 收紧），与 god / dupe 两门同形态。

为什么逐规则而不是看总数：总数会让"某一类暴涨、另一类修好"互相抵消——
这正是快照棘轮那套"六类绕过"里最常见的漏法。

工具缺失**不静默**：ruff 不在 PATH 直接 FAIL（CI 由 `ci-requirements.txt` 钉版本装）。

用法：
  python -X utf8 scripts/lint_gate.py                     # 判红（默认 root=.）
  python -X utf8 scripts/lint_gate.py --write-baseline    # 记录/收紧基线
  python -X utf8 scripts/lint_gate.py --list               # 打逐规则表
  python -X utf8 scripts/lint_gate.py --root <仓> --baseline <路径>
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

BASELINE_NAME = "lint-baseline.json"
_DOC = ("Python 静态门基线（脚本 scripts/lint_gate.py）：规则号 → 命中数。"
        "棘轮只准减；涨一个或冒出新规则号即红。")


def run_ruff(root: pathlib.Path) -> list[tuple[str, str, int]]:
    """跑 ruff（自动加载 root 下的 ruff.toml），返回 [(code, file, line)]。"""
    exe = shutil.which("ruff")
    if not exe:
        raise RuntimeError(
            "ruff 不可用（PATH 里没有）——本地 `pip install ruff==0.16.2`，"
            "CI 由 .github/ci-requirements.txt 安装（红线：缺工具不静默降级）")
    p = subprocess.run([exe, "check", str(root), "--output-format=json", "--quiet"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(root))
    if p.returncode not in (0, 1):              # 0=干净 1=有命中；其余是 ruff 自己坏了
        raise RuntimeError(f"ruff 异常退出 {p.returncode}: {(p.stderr or '')[:300]}")
    try:
        items = json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ruff 输出不是 JSON（{e}）：{(p.stdout or '')[:200]}")
    out = []
    for it in items:
        loc = it.get("location") or {}
        out.append((str(it.get("code") or "?"), str(it.get("filename") or ""),
                    int(loc.get("row") or 0)))
    return out


def count_by_rule(items: list[tuple[str, str, int]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code, _f, _l in items:
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def load_baseline(path: pathlib.Path) -> dict[str, int]:
    """读基线：不存在 ⇒ {}（并在 stderr 提示）；解析不了 ⇒ 明确报错退出。"""
    if not path.is_file():
        print(f"[lint-gate] 尚无基线 {path.name} ⇒ 只判有无命中，请跑 --write-baseline",
              file=sys.stderr)
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[lint-gate] 基线坏了（{path}）：{e} —— 修好或重跑 --write-baseline",
              file=sys.stderr)
        raise SystemExit(2)
    counts = doc.get("counts") if isinstance(doc, dict) else None
    if not isinstance(counts, dict):
        print(f"[lint-gate] 基线格式不对（缺 counts）：{path}", file=sys.stderr)
        raise SystemExit(2)
    return {str(k): int(v) for k, v in counts.items()}


def write_baseline(path: pathlib.Path, counts: dict[str, int]) -> None:
    """原子写（temp + os.replace）：中断不会留半截 JSON（god 门踩过的自锁）。"""
    payload = json.dumps({"_doc": _DOC, "counts": counts}, ensure_ascii=False,
                         indent=1) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    print(f"[lint-gate] 已写基线 {path}（{len(counts)} 条规则，命中 {sum(counts.values())}）")


def evaluate(base: dict[str, int], cur: dict[str, int]) -> tuple[list[str], list[str]]:
    """(红, 可收紧)。涨一条或冒出新规则号都算红。"""
    bad, shrank = [], []
    for code, n in sorted(cur.items()):
        b = base.get(code)
        if b is None:
            bad.append(f"{code}: 新规则号 {n} 条（基线里没有）")
        elif n > b:
            bad.append(f"{code}: {b} → {n}（涨 {n - b}）")
        elif n < b:
            shrank.append(f"{code}: {b} → {n}（可收紧）")
    for code, b in sorted(base.items()):
        if code not in cur:
            shrank.append(f"{code}: {b} → 0（可收紧）")
    return bad, shrank


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    bpath = pathlib.Path(a.baseline) if a.baseline else root / BASELINE_NAME

    try:
        items = run_ruff(root)
    except RuntimeError as e:
        print(f"FAIL lint-gate {e}")
        return 1
    cur = count_by_rule(items)

    print(f"LINT-GATE root={root} 规则={len(cur)} 命中={sum(cur.values())} "
          f"基线={bpath.name}")
    if a.list:
        for code, n in sorted(cur.items(), key=lambda kv: -kv[1]):
            print(f"  {n:6d}  {code}")
        return 0

    if a.write_baseline:
        write_baseline(bpath, cur)
        return 0

    base = load_baseline(bpath)
    bad, shrank = evaluate(base, cur)
    for line in shrank:
        print(f"  ⇄ {line}")
    if bad:
        for line in bad:
            print(f"  ✗ {line}")
        print(f"LINT-GATE FAIL 新增/变多={len(bad)}")
        return 1
    print(f"  （{len(shrank)} 条可收紧，跑 --write-baseline 更新）" if shrank else "  （无变化）")
    print(f"LINT-GATE OK 命中={sum(cur.values())} 规则={len(cur)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
