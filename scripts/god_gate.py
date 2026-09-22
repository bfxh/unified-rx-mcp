#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上帝对象门（god_gate）——**可移植**：零依赖、配置驱动、带棘轮基线，任何项目直接抄走。

**为什么要有它**（用户定调：任何项目都不许有上帝对象）：文件/函数/类型一旦长成庞然大物，
改一处要读懂一整屏、并行化与测试都下不去手；靠人盯必然漏 ⇒ 机器门 + 只准减的基线。

**可移植性**：本文件不 import 任何项目内模块、不假定目录结构；把 `god_gate.py` +
一份 `god.gate.json`（阈值/包含排除）抄到别的仓即可用。语言支持按**成本/收益**分级：
  · Python —— `ast` 精确（函数/方法/类字段/行数）；
  · Rust / JS / TS —— 大括号深度启发式（fn / impl / struct / function / class），**输出标注 heuristic**；
  · 其它语言 —— 只量文件行数（仍有用：巨文件是上帝对象的必要条件）。

**棘轮语义**（与快照棘轮门同款）：`--write-baseline` 生成基线后，
  · 任何指标**超过**基线值 ⇒ 红（不许变胖）；
  · 低于基线 ⇒ 绿，并提示可 `--write-baseline` 收紧（**只准减**）；
  · 新文件/新函数超阈值 ⇒ 红（没有基线可依赖）；
  · 基线里的条目消失 ⇒ 提示清理（不算红）。

用法：
  python god_gate.py --root . --write-baseline      # 首次：记录现状为基线
  python god_gate.py --root .                       # 门：只报"变胖/新增超阈"
  python god_gate.py --root . --top 20 --list       # 看看谁最胖（不判红）
退出码：0 = 通过；1 = 有超标/变胖；2 = 用法或配置错。
"""
import argparse
import ast
import json
import os
import pathlib
import re
import sys

DEFAULT_CFG = {
    "max_file_lines": 800,
    "max_fn_lines": 120,
    "max_type_members": 24,          # 类/Rust impl 的方法+字段数
    "include": ["**/*.py", "**/*.rs", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.mjs", "**/*.cjs"],
    "exclude": ["**/.git/**", "**/node_modules/**", "**/target/**", "**/__pycache__/**",
                "**/dist/**", "**/build/**", "**/.venv/**", "**/venv/**", "**/*.min.*"],
    "baseline": "god-baseline.json",
}
PY = {".py"}
BRACE = {".rs", ".js", ".ts", ".tsx", ".mjs", ".cjs"}


def load_cfg(root: pathlib.Path, cfg_path: str | None) -> dict:
    cfg = dict(DEFAULT_CFG)
    p = pathlib.Path(cfg_path) if cfg_path else root / "god.gate.json"
    if p.is_file():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def included(rel: str, cfg: dict) -> bool:
    """包含/排除匹配。**fnmatch 的坑**：`**/x` 不匹配根级 `x`（首版因此把 server.py/registry.py
    这类根文件静默漏掉——门有盲区，靠"基线里查不到 server.py"才发现）。故 `**/` 前缀一律可省。"""
    import fnmatch

    def hit(pat: str) -> bool:
        return fnmatch.fnmatch(rel, pat) or (pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]))

    if any(hit(p) for p in cfg["exclude"]):
        return False
    return any(hit(p) for p in cfg["include"])


def py_metrics(src: str) -> list[tuple[str, int, str]]:
    """Python：ast 精确 ⇒ [(名字, 行数, 种类)]（种类 ∈ fn/type）。"""
    out: list[tuple[str, int, str]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, (node.end_lineno or node.lineno) - node.lineno + 1, "fn"))
        elif isinstance(node, ast.ClassDef):
            members = sum(isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) for b in node.body)
            members += sum(len(b.targets) for b in node.body if isinstance(b, ast.Assign))
            out.append((node.name, members, "type"))
    return out


def brace_metrics(src: str) -> list[tuple[str, int, str]]:
    """Rust/JS 启发式：正则找声明起点 + 花括号配平到闭合（字符串/注释不剔除，误差已在文档标注）。

    两类指标分开算，**别把行数当成员数**（首版就犯了这个错）：
      · fn  → 函数体行数；
      · type→ 深度 1 上的成员数（`fn` 行 + 字段声明行）。
    """
    out: list[tuple[str, int, str]] = []
    pat = re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|function)\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"|^\s*(?:pub\s+)?(?:impl|trait|struct|enum|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
    fn_line = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+[A-Za-z_]")
    field_line = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?[A-Za-z_][A-Za-z0-9_]*\s*:\s*\S")
    lines = src.splitlines()
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        name = m.group(1) or m.group(2)
        kind = "fn" if m.group(1) else "type"
        depth, started, end = 0, False, i
        for j in range(i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                started = True
            if started and depth <= 0:
                end = j
                break
        if kind == "fn":
            out.append((name, end - i + 1, "fn"))
        else:
            # 成员数：块内深度 1 的 fn / 字段行（启发式，量级正确即可）
            depth = 0
            members = 0
            for j in range(i, end + 1):
                d0 = depth
                depth += lines[j].count("{") - lines[j].count("}")
                if j == i:
                    continue
                if d0 == 1 and (fn_line.match(lines[j]) or field_line.match(lines[j])):
                    members += 1
            out.append((name, members, "type"))
    return out


def scan(root: pathlib.Path, cfg: dict) -> dict:
    """返回 {relpath: {"file_lines": n, "max_fn_lines": m, "max_type_members": k, "hot": "名字"}}。"""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "target",
                                                        "__pycache__", "dist", "build", ".venv", "venv"}]
        for fn in filenames:
            fp = pathlib.Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            if not included(rel, cfg):
                continue
            try:
                src = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            metrics = py_metrics(src) if fp.suffix in PY else (
                brace_metrics(src) if fp.suffix in BRACE else [])
            fns = [m for m in metrics if m[2] == "fn"]
            types = [m for m in metrics if m[2] == "type"]
            files[rel] = {
                "file_lines": src.count("\n") + 1,
                "max_fn_lines": max((n for _, n, _ in fns), default=0),
                "max_type_members": max((n for _, n, _ in types), default=0),
                "hot": (max(metrics, key=lambda m: m[1])[0] if metrics else ""),
                "heuristic": fp.suffix not in PY,
            }
    return files


def evaluate(files: dict, base: dict, cfg: dict) -> tuple[list[str], list[str], list[str]]:
    bad, grew, shrank = [], [], []
    for rel, m in sorted(files.items()):
        b = base.get(rel)
        for key, lim in (("file_lines", cfg["max_file_lines"]),
                         ("max_fn_lines", cfg["max_fn_lines"]),
                         ("max_type_members", cfg["max_type_members"])):
            v = m[key]
            bv = (b or {}).get(key, 0)
            if b is None:                                   # 新文件：只看阈值
                if v > lim:
                    bad.append(f"{rel}: {key}={v} > {lim}（新增，无基线）")
            elif v > bv:                                    # 变胖：红线（不看阈值）
                bad.append(f"{rel}: {key} {bv} → {v}（不许变胖）")
            elif v < bv:
                shrank.append(f"{rel}: {key} {bv} → {v}（可收紧基线）")
    for rel in base:
        if rel not in files:
            shrank.append(f"{rel}: 基线条目已消失（可清理）")
    return bad, grew, shrank


def load_baseline(path: pathlib.Path) -> dict:
    """读基线：不存在 ⇒ {}；**存在但解析不了 ⇒ 报明确错误并退出**。

    为什么值得单列（实测）：首版直接 json.loads，一次**被中断的写入**留下半截文件后，
    门抛的是裸 JSONDecodeError（看不出是基线坏了）。这里把话说明白，并提示怎么恢复。
    """
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"错误：基线 {path.name} 不是合法 JSON（{e}）"
              f"——若上次 --write-baseline 被中断，重跑一次即可（写入已改原子替换）")
        sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    cfg = load_cfg(root, a.config)
    bpath = pathlib.Path(a.baseline) if a.baseline else root / cfg["baseline"]
    files = scan(root, cfg)
    # 注意：**别在这里读基线**——`--write-baseline` 不需要旧基线，先读会让"基线坏了"变成自锁
    # （实测：一次中断写入把基线截断，于是连修复用的 --write-baseline 也被拒）。
    # 读取推迟到 evaluate 之前。

    hot = sorted(files.items(), key=lambda kv: -max(kv[1]["file_lines"] / cfg["max_file_lines"],
                                                   kv[1]["max_fn_lines"] / cfg["max_fn_lines"]))[:a.top]
    print(f"GOD-GATE root={root} 文件={len(files)} 阈值: file>{cfg['max_file_lines']}行 "
          f"fn>{cfg['max_fn_lines']}行 type>{cfg['max_type_members']}成员 基线={bpath.name}"
          f"{'(heuristic 语言已标注)' if any(m['heuristic'] for m in files.values()) else ''}")
    for rel, m in hot:
        flag = "H" if m["heuristic"] else " "
        print(f"  {flag} 文件{m['file_lines']:6d}行  最长函数{m['max_fn_lines']:5d}行  "
              f"最大类型{m['max_type_members']:3d}成员  {rel}  （最大块: {m['hot']}）")

    if a.list:
        return 0
    if a.write_baseline:
        payload = json.dumps({k: {kk: v[kk] for kk in
                                  ("file_lines", "max_fn_lines", "max_type_members")}
                              for k, v in sorted(files.items())},
                             ensure_ascii=False, indent=1) + "\n"
        tmp = bpath.with_suffix(bpath.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, bpath)          # 原子替换：中断也不会留下半截基线
        print(f"已写基线 {bpath}（{len(files)} 个文件）——此后只准减")
        return 0

    base = load_baseline(bpath)          # 到这里才读（见上：写基线/列清单都不该被坏基线挡住）
    bad, _grew, shrank = evaluate(files, base, cfg)
    if not base:
        print("警告：尚无基线 ⇒ 只对「新文件」判阈值；先跑 --write-baseline 才会管住存量")
    for line in bad[:30]:
        print(f"  ✗ {line}")
    if len(bad) > 30:
        print(f"  …另有 {len(bad)-30} 条")
    if shrank:
        print(f"  （{len(shrank)} 条可收紧/可清理，跑 --write-baseline 更新）")
    print(f"GOD-GATE {'FAIL' if bad else 'OK'} 超标/变胖={len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
