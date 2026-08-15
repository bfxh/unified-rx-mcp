# -*- coding: utf-8 -*-
"""patch_learn：从漏洞修复 diff 提取模式 → 生成检测规则。

KNighter（SOSP 2025）概念轻量版：让"补丁学习"产出确定性规则——
本实现从标准 diff 的 `-` 行（修复前漏洞代码）提取含危险 sink 的行，
正则化（字面量/变量→\\w+，sink 调用名保留）→ 输出可直接加入
vuln_rules.json 的规则建议。零依赖、确定性、不依赖 LLM。
"""

import re


_SINKS = ("execute", "query", "eval", "system", "Popen", "open",
          "loads", "urlopen", "render", "join", "subprocess")


def patch_learn(diff_text: str, language: str = ".py") -> dict:
    """从修复 diff 提取漏洞模式 → 规则建议。

    diff 输入：标准统一 diff（`-` 行 = 修复前漏洞代码）。
    返回 {ok, extracted, rules: [{pattern, suggested_msg, source_line}]}。
    """
    if not diff_text or len(diff_text) > 200_000:
        return {"ok": False, "error": "diff 需 1..200000 字符"}
    removed = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            body = line[1:].strip()
            if any(s in body for s in _SINKS):
                removed.append(body)
    if not removed:
        return {"ok": False,
                "error": "diff 中未找到含危险 sink 的删除行（无规则可提取）"}
    rules = []
    for body in removed[:8]:
        rx = _regularize(body)
        rules.append({
            "pattern": rx,
            "suggested_msg": f"疑似漏洞模式（从修复 diff 学得）：{body[:60]}",
            "source_line": body[:80],
        })
    return {"ok": True, "extracted": len(removed), "rules": rules,
            "advice": "规则可加入 vuln_rules.json（id/pattern/language/"
                      "severity/msg）——确定性规则规模化（KNighter 概念）"}


def _regularize(body: str) -> str:
    """正则化：字符串/数字→占位；sink 名保留；其余变量→\\w+；转义特殊字符。

    结果可直接 re.compile——漏洞代码命中、修复代码（参数化/安全写法）
    不命中（sink 调用结构保留 + 字面量占位）。
    """
    rx = re.sub(r"'[^']*'|\"[^\"]*\"", "@@STR@@", body)
    rx = re.sub(r"\b\d+\b", r"\\d+", rx)
    keep = "|".join(_SINKS)
    rx = re.sub(r"\b(?!(?:" + keep + r")\b)[a-z_]\w*\b", r"\\w+", rx)
    # 占位保护量词（\w+/\d+ 整体——防 + 被误转义为字面）
    rx = rx.replace(r"\w+", "@@W@@").replace(r"\d+", "@@D@@")
    # 特殊字符转义（先保护占位符反斜杠；@@STR@@ 无特殊字符）
    rx = rx.replace("\\", "@@BS@@")
    for ch in "().+*?[]{}|^$":
        rx = rx.replace(ch, "\\" + ch)
    rx = rx.replace("@@BS@@", "\\")
    rx = rx.replace("@@W@@", r"\w+").replace("@@D@@", r"\d+")
    # 字符串占位最后恢复（字符类括号不被转义）
    rx = rx.replace("@@STR@@", r"['\"][^'\"]*['\"]")
    return rx[:120]
