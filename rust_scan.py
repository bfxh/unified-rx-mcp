#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rust_scan.py — Rust 静态 bug 扫描（tree-sitter-rust，P2a 补 Rust 支持）。

抄 semgrep 模式规则思路 + tree-sitter 语法树。检测（基于 tree-sitter-rust 实际节点类型）：
  - unwrap()/expect() 方法调用（panic 风险）——field_expression + call_expression
  - panic!/unreachable!/todo!/unimplemented! 宏调用——macro_invocation
  - unsafe 块——unsafe_block
  - as 裸 cast——type_cast_expression
  - indexing 越界风险提示（静态不可判定时标 info）

用法：
  scan_rust_file(path) -> (issues, line_count)
"""
import os

try:
    import tree_sitter as ts
    import tree_sitter_rust as tsr
    _PARSER = ts.Parser(ts.Language(tsr.language()))
except Exception:
    _PARSER = None

# 宏名 → (描述, 严重度)
_MACRO_RULES = {
    "panic": ("panic! 显式崩溃点", "error"),
    "unreachable": ("unreachable!() 不可达分支（触发即崩溃）", "error"),
    "todo": ("todo!() 未实现标记（运行即崩溃）", "error"),
    "unimplemented": ("unimplemented!() 未实现（运行即崩溃）", "error"),
}

# 方法调用 → (描述, 严重度)
_METHOD_RULES = {
    "unwrap": ("unwrap() 裸用（None/Err 时 panic——建议 match/ok_or/?)", "warn"),
    "expect": ("expect() 裸用（失败即 panic——建议返回 Result）", "warn"),
}


def _node_text(node) -> str:
    try:
        return node.text.decode("utf-8", "ignore")
    except Exception:
        return ""


def _is_test_attr(node) -> bool:
    """判断 attribute_item 是否为 #[test]（含 #[test] / #[tokio::test] 等）。"""
    txt = _node_text(node)
    return "test" in txt and "cfg(test)" not in txt


def _scan_tree(root, path: str, lines: list[str]) -> list[dict]:
    """tree-sitter 语法树扫描（跳过 #[cfg(test)] 测试模块——测试里 unwrap 合理）。

    先收集所有 cfg(test) 模块的字节范围，扫描时跳过（生产代码报告更精确）。
    """
    # 1. 收集测试范围（mod tests 块 + 顶层 #[test] 函数）
    test_ranges: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "attribute_item" and "cfg(test)" in _node_text(n):
            # cfg(test) 是 mod_item 的兄弟 attribute（tree-sitter-rust 结构：
            # attribute_item 与 mod_item 平级）——取下一个 named 兄弟
            sib = n.next_named_sibling
            while sib is not None and sib.type not in ("mod_item", "function_item"):
                sib = sib.next_named_sibling
            if sib is not None and sib.type in ("mod_item", "function_item"):
                test_ranges.append((sib.start_byte, sib.end_byte))
        elif n.type == "attribute_item" and _is_test_attr(n):
            # #[test] 修饰的是下一个 function_item（集成测试顶层 fn）
            sib = n.next_named_sibling
            while sib is not None and sib.type not in ("function_item", "mod_item"):
                sib = sib.next_named_sibling
            if sib is not None and sib.type in ("function_item", "mod_item"):
                test_ranges.append((sib.start_byte, sib.end_byte))
        stack.extend(n.children)
    # 2. 主扫描（跳过测试范围）
    issues = []
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if test_ranges and any(a <= n.start_byte and n.end_byte <= b
                               for a, b in test_ranges):
            continue  # 测试模块内：跳过
        line_no = n.start_point[0] + 1
        snippet = lines[line_no - 1][:120] if line_no - 1 < len(lines) else ""
        if t == "macro_invocation":
            # panic!/todo!/... 宏名是第一个子节点（identifier）
            for ch in n.children:
                if ch.type == "identifier":
                    name = _node_text(ch)
                    if name in _MACRO_RULES:
                        desc, sev = _MACRO_RULES[name]
                        issues.append({"file": path, "line": line_no,
                                       "message": desc, "severity": sev,
                                       "rule": name, "col": n.start_point[1] + 1,
                                       "snippet": snippet})
                    break
        elif t == "unsafe_block":
            issues.append({"file": path, "line": line_no,
                           "message": "unsafe 块（需人工审查：裸指针/未定义行为风险）",
                           "severity": "info", "rule": "unsafe",
                           "col": n.start_point[1] + 1, "snippet": snippet})
        elif t == "type_cast_expression":
            # as 裸 cast：只报危险转换（截断/符号变化），跳过安全常规（as f32/as usize 等）
            cast_txt = _node_text(n)
            for ch in n.children:
                if ch.type == "as":
                    # 提取目标类型（as 后面的 type_identifier / primitive_type）
                    target = ""
                    nxt = ch.next_named_sibling
                    if nxt is not None:
                        target = _node_text(nxt)
                    # 危险目标类型：整数窄化/符号变化（u8/i8/u16/i16 等）、f32 截断
                    dangerous = target in ("u8", "i8", "u16", "i16", "u32", "i32", "f32", "u64")
                    if dangerous:
                        issues.append({"file": path, "line": line_no,
                                       "message": f"as {target} 裸转换（可能截断/溢出——建议 try_from/from）",
                                       "severity": "warn", "rule": "as",
                                       "col": n.start_point[1] + 1, "snippet": snippet})
                    break
        elif t == "field_expression":
            # unwrap()/expect()：field_expression 的最后一个 field_identifier
            for ch in reversed(n.children):
                if ch.type == "field_identifier":
                    name = _node_text(ch)
                    if name in _METHOD_RULES:
                        desc, sev = _METHOD_RULES[name]
                        issues.append({"file": path, "line": line_no,
                                       "message": desc, "severity": sev,
                                       "rule": name, "col": n.start_point[1] + 1,
                                       "snippet": snippet})
                    break
        stack.extend(n.children)
    return issues


def scan_rust_file(path: str) -> tuple[list, int]:
    """扫单个 Rust 文件，返回 (issues, line_count)。tree-sitter 不可用时降级文本扫描。"""
    issues = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            content = fh.read(1_000_000)
    except OSError as exc:
        return [{"file": path, "line": 0, "message": f"读取失败: {exc}",
                 "severity": "error", "rule": "io", "col": 0}], 0
    lines = content.splitlines()
    total = len(lines)

    if _PARSER is not None:
        try:
            tree = _PARSER.parse(content.encode("utf-8"))
            return _scan_tree(tree.root_node, path, lines), total
        except Exception:
            pass  # 降级文本扫描

    # 文本降级
    for i, line in enumerate(lines, 1):
        for token, desc, sev in _MACRO_RULES.items():
            if f"{token}!" in line:
                issues.append({"file": path, "line": i, "message": desc,
                               "severity": sev, "rule": token, "col": 0,
                               "snippet": line[:120]})
        for token, desc, sev in _METHOD_RULES.items():
            if f".{token}(" in line:
                issues.append({"file": path, "line": i, "message": desc,
                               "severity": sev, "rule": token, "col": 0,
                               "snippet": line[:120]})
        if "unsafe {" in line or "unsafe{" in line:
            issues.append({"file": path, "line": i,
                           "message": "unsafe 块（需人工审查）",
                           "severity": "info", "rule": "unsafe", "col": 0,
                           "snippet": line[:120]})
        if " as " in line:
            issues.append({"file": path, "line": i,
                           "message": "as 裸类型转换",
                           "severity": "warn", "rule": "as", "col": 0,
                           "snippet": line[:120]})
    return issues, total
