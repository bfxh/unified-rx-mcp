#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/commands.py — 对话行解析 / LLM 上下文构建 / LLM 编辑提取（纯逻辑）。"""
import re

# 函数定位复用 ide_fusion 的多语言正则（包导入时项目根已进 sys.path）
try:
    from ide_fusion import _FN_RE
except Exception:  # 降级：无函数定位能力（窗口兜底仍可用）
    _FN_RE = None

_WINDOW = 20  # 定位失败兜底：选区/光标行上下各 20 行
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def parse(text: str) -> dict:
    """对话行意图解析：`>` 命令 / /explain / /fix / 其余编辑。

    斜杠命令按首个空白切 token 精确比较（词边界）——
    `/explains foo` 等不误判为 explain。
    """
    t = (text or "").strip()
    if t.startswith(">"):
        return {"kind": "term", "body": t[1:].strip()}
    parts = t.split(None, 1)
    head = parts[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if head == "/explain":
        return {"kind": "explain", "body": rest}
    if head == "/fix":
        return {"kind": "fix", "body": rest}
    return {"kind": "edit", "body": t}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _strip_noise(line: str) -> str:
    """粗略剥离字符串内容与行注释（防 `printf(")")` 等干扰括号平衡）。"""
    out: list[str] = []
    i, n = 0, len(line)
    quote = ""
    while i < n:
        ch = line[i]
        if quote:  # 字符串内：跳到闭引号（认转义）
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            continue
        if ch == "#":  # py/shell 行注释
            break
        if ch == "/" and i + 1 < n and line[i + 1] == "/":  # c 系行注释
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _brace_delta(line: str) -> int:
    """单行（已剥离字符串/注释）括号净增量。"""
    return sum(1 for ch in line if ch in "({[") - sum(1 for ch in line if ch in ")}]")


def _estimate_fn_end(lines: list[str], start: int) -> int:
    """估计函数结束行（0-based）：括号平衡 + 缩进回退结合。

    括号按剥离字符串/注释后计数；归零后若下一非空行缩进仍大于基准
    （Python 多行签名的函数体等）不按括号语言截断——转缩进规则延伸。
    """
    base = _indent(lines[start])
    bal = _brace_delta(_strip_noise(lines[start]))
    scan_from = start + 1
    if bal > 0:  # 首行开括号未闭合：先找归零行
        closed = -1
        for j in range(start + 1, len(lines)):
            bal += _brace_delta(_strip_noise(lines[j]))
            if bal <= 0:
                closed = j
                break
        if closed < 0:
            return len(lines) - 1
        k = closed + 1  # 归零行后的下一非空行
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k >= len(lines) or _indent(lines[k]) <= base:
            return closed  # 括号语言：结束于归零行
        scan_from = closed + 1  # 缩进仍深于基准 → 转缩进规则继续
    for j in range(scan_from, len(lines)):  # 缩进规则：回退到基准 → 止于上一行
        if lines[j].strip() and _indent(lines[j]) <= base:
            return j - 1
    return len(lines) - 1


def build_context(file_text: str, cursor_line: int, selection: dict | None = None,
                  full: bool = False) -> dict:
    """构建 LLM 上下文：full 全文 > 光标所在函数 > 选区/光标行 ±20 行。

    返回 {"context_text", "line_count", "fn_name"}（selection 为 1-based 行号）。
    嵌套函数：内层估计尾未覆盖光标时继续向上找外层函数。
    """
    lines = (file_text or "").splitlines()
    n = len(lines)
    if full:
        return {"context_text": file_text or "", "line_count": n, "fn_name": None}
    cur = max(1, min(cursor_line or 1, n)) if n else 1
    # 向上扫描定位包含光标的函数起始行（ide_fusion._FN_RE）
    if n and _FN_RE is not None:
        for i in range(cur - 1, -1, -1):
            m = _FN_RE.match(lines[i])
            if not m:
                continue
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            end = _estimate_fn_end(lines, i)
            # 估计尾未覆盖光标（如嵌套函数已过内层）→ 继续向上找外层
            if end + 1 < cur:
                continue
            seg = lines[i:end + 1]
            return {"context_text": "\n".join(seg), "line_count": len(seg),
                    "fn_name": name}
    # 兜底：选区（或光标行）上下各 20 行
    if selection and selection.get("start") and selection.get("end"):
        lo, hi = int(selection["start"]), int(selection["end"])
    else:
        lo = hi = cur
    lo = max(1, lo - _WINDOW)
    hi = min(n, hi + _WINDOW)
    seg = lines[lo - 1:hi]
    return {"context_text": "\n".join(seg), "line_count": len(seg), "fn_name": None}


def parse_llm_edit(reply: str) -> str | None:
    """从 LLM 回复提取最后一个围栏代码块（```lang ... ```）；无代码块返回 None。

    纯围栏提取（评审：server 解析通道契约全错且有副作用——已移除）。
    """
    ms = _FENCE_RE.findall(reply or "")
    if not ms:
        return None
    code = ms[-1]
    return code[:-1] if code.endswith("\n") else code
