#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_fusion.py — IDE 结果融合进掌握引擎（IDE_ENHANCE_PLAN R6）。

融合三路：
  1. 诊断 → 符号图：bug_scan/quality_scan 问题按符号归属标注（图节点带问题）
  2. IDE 查询 → 教训语料：lsp_query/code_context 高频对象记录为候选教训
  3. 影响面双引擎校验：change_impact（LSP/词级）vs 符号图 callers（tree-sitter）对比
"""

import json
import os
import time
import re

# 符号归属启发式：行号 → 所在函数（tree-sitter 降级：正则 fn/def 扫描）
_FN_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"          # rs
    r"|^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\b"                                 # py
    r"|^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("            # go/gd
    r"|^\s*(?:export\s+)?(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*))"  # ts/js
    r"|^\s*(?:static\s+|inline\s+|extern\s+)*(?!return\b|if\b|while\b|for\b)" # c/cpp
    r"[A-Za-z_]\w*\s+([A-Za-z_]\w*)\s*\(")


def annotate_issues(root: str, issues: list[dict]) -> dict:
    """问题列表 → 按文件+符号聚合（诊断标注到符号图）。

    issues: [{file, line, kind, message}]（bug_scan/quality_scan 输出格式）
    返回 {symbol_map: {file#symbol: count}, by_file: {...}, total: n}
    """
    _t0 = time.perf_counter()
    fn_lines: dict[str, list[tuple[int, str]]] = {}
    by_symbol: dict[str, int] = {}
    by_file: dict[str, int] = {}
    by_rule: dict[str, int] = {}  # IDE 增强 100：规则分布（问题类型一眼可见）
    by_sev: dict[str, int] = {}   # IDE 增强 110：严重度分布（优先级一眼可见）

    def load_fn_lines(path: str) -> list[tuple[int, str]]:
        if path in fn_lines:
            return fn_lines[path]
        result: list[tuple[int, str]] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    m = _FN_RE.match(line)
                    if m:
                        name = next((g for g in m.groups() if g), "")
                        if name:
                            result.append((i, name))
        except OSError:
            pass
        fn_lines[path] = result
        return result

    for iss in issues:
        path = iss.get("file", "")
        line = iss.get("line", 0)
        by_file[path] = by_file.get(path, 0) + 1
        # IDE 增强 100：规则分布（AI 判断修复优先级——什么类型问题最多）
        rule = str(iss.get("rule") or iss.get("kind") or "unknown")[:40]
        by_rule[rule] = by_rule.get(rule, 0) + 1
        # IDE 增强 110：严重度分布（error/warn/info 计数——优先级一眼可见）
        sev = str(iss.get("severity") or "").lower()
        if sev not in ("error", "warning", "warn", "info", "suggestion"):
            sev = "unknown"
        by_sev[sev] = by_sev.get(sev, 0) + 1
        symbol = "<unknown>"
        cur = None
        for ln, name in load_fn_lines(path):
            if ln <= line:
                cur = name
            else:
                break
        if cur:
            symbol = cur
        key = f"{path}#{symbol}"
        by_symbol[key] = by_symbol.get(key, 0) + 1

    return {
        "ok": True,
        "total": len(issues),
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "severity_counts": dict(sorted(by_sev.items(), key=lambda kv: -kv[1])),
        "symbol_map": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])[:50]),
        # IDE 增强 132：top 符号建议（问题最集中的符号——重点排查入口）
        "top_symbols_advice": [
            {"symbol": sym.split("#")[-1], "file": sym.split("#")[0],
             "issues": cnt}
            for sym, cnt in sorted(by_symbol.items(), key=lambda kv: -kv[1])[:5]
        ],
        # IDE 增强 246：融合耗时（ms——收官；由 server 包装层注入）
        "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
    }


def cross_validate_impact(repo_path: str, symbol: str,
                          lsp_refs: list[str], tree_refs: list[str]) -> dict:
    """双引擎校验：LSP 引用 vs tree-sitter 调用（符号图）。

    lsp_refs/tree_refs 都是文件路径列表。返回交集/差异——两者不一致处是
    潜在漏检或过检（重命名安全性验证用）。
    """
    lsp_set = set(lsp_refs)
    tree_set = set(tree_refs)
    return {
        "ok": True,
        "symbol": symbol,
        "lsp_only": sorted(lsp_set - tree_set),   # LSP 报但图没有 → 可能图缺边
        "tree_only": sorted(tree_set - lsp_set),  # 图报但 LSP 没有 → 可能 LSP 漏
        "both": sorted(lsp_set & tree_set),
        "lsp_count": len(lsp_set),
        "tree_count": len(tree_set),
        "verdict": ("一致" if lsp_set == tree_set
                    else f"差异 {len(lsp_set ^ tree_set)} 处——重命名前需人工确认"),
    }


def impact_via_references(repo_path: str, symbol: str,
                          lsp_refs: list[str]) -> dict:
    """双引擎校验便捷入口（IDE 增强四 2026-08-13）：tree 侧引用数据来自
    ide_references（词级 + 声明判定，注释/字符串排除）——无独立符号图也能跑。

    lsp_refs：LSP 引用（文件路径列表，可空——LSP 不可用时全由 tree 侧提供）。
    返回 cross_validate_impact 的完整结果 + tree 侧引用明细。
    """
    from ide_tools import ide_references
    r = ide_references(repo_path, symbol)
    if not r.get("ok"):
        return {"ok": False, "symbol": symbol,
                "error": r.get("error", "ide_references 失败")}
    tree_files = sorted({ref["file"] for ref in
                         r.get("definitions", []) + r.get("references", [])})
    result = cross_validate_impact(repo_path, symbol,
                                   lsp_refs or [], tree_files)
    result["tree_refs"] = tree_files
    result["definition_count"] = r.get("definition_count", 0)
    result["reference_count"] = r.get("reference_count", 0)
    result["source"] = "tree=ide_references(词级+声明判定，注释/字符串排除)"
    return result


def record_ide_usage(lesson_dir: str, tool: str, target: str, outcome: dict) -> dict:
    """IDE 查询记录 → 教训库候选（高频对象沉淀为教训）。"""
    try:
        os.makedirs(lesson_dir, exist_ok=True)
    except OSError:
        return {"ok": False, "error": f"教训目录不可建: {lesson_dir}"}
    rec = {"tool": tool, "target": target, "outcome": outcome}
    try:
        path = os.path.join(lesson_dir, "ide_usage.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "recorded": rec}
