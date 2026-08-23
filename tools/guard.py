# -*- coding: utf-8 -*-
"""tools/guard.py —— 防幻觉域（2 工具）：hallucination_guard / capability_manifest

AI 声明事实核查（verified/refuted/unverifiable 三分级）——防 AI 编造
file:line / 符号 / 工具名。这是"工具代替智能体"里最关键的护栏。
"""
import os
import re

from registry import tool, list_tools

_CAPABILITIES = {
    "有": [
        "本地文件读写（沙盒内）", "静态 bug 扫描（多语言）", "工程标准检查",
        "代码定位/上下文/编辑", "语义检索", "防幻觉核查", "教训记忆",
        "成本统计", "备份", "游戏域检查", "纯函数计算", "命令执行（白名单）",
    ],
    "没有": [
        "联网搜索/网页抓取", "任意代码执行（白名单外）", "沙盒外路径访问",
        "本地模型推理（暂未接入）", "GitHub 直接访问（被墙）",
    ],
}


@tool("capability_manifest", "能力边界清单（有什么/没有什么，防能力幻觉）", "guard",
      {"type": "object", "properties": {}, "required": []})
def capability_manifest():
    tools = list_tools()
    groups = {}
    for t in tools:
        g = t.get("_group", "misc")
        groups.setdefault(g, []).append(t["name"])
    return {
        "定位": "工具箱，不是智能体；产出证据与事实，不替代 LLM 推理",
        "有": _CAPABILITIES["有"],
        "没有": _CAPABILITIES["没有"],
        "工具面": f"{len(tools)} 工具",
        "分组": groups,
    }


@tool("hallucination_guard", "声明核查：file:line/符号/工具名 → verified/refuted/unverifiable", "guard",
      {"type": "object",
       "properties": {
           "text": {"type": "string", "description": "AI 声明文本（含 file:line / 反引号符号）"},
           "root": {"type": "string", "description": "仓库根目录（相对路径解析基准，可选）"},
       },
       "required": ["text"]})
def hallucination_guard(text, root=None):
    root = root or os.getcwd()
    tool_names = {t["name"] for t in list_tools()}
    results = []

    # 1. 工具名声明（反引号）
    for m in re.finditer(r"`([a-z][a-z0-9_]{2,})`", text):
        name = m.group(1)
        if name in tool_names:
            results.append({"decl": m.group(0), "kind": "tool", "status": "verified",
                            "detail": f"工具存在: {name}"})
        else:
            results.append({"decl": m.group(0), "kind": "tool", "status": "refuted",
                            "detail": f"工具不存在: {name}"})

    # 2. file:line 声明
    for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.(?:py|rs|go|ts|js|gd|cs|dart|java|kt|rb|php))(?::(\d+))?", text):
        fpath, lineno = m.group(1), m.group(2)
        full = fpath if os.path.isabs(fpath) else os.path.join(root, fpath)
        if os.path.isfile(full):
            if lineno:
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        n = sum(1 for _ in f)
                    status = "verified" if int(lineno) <= n else "refuted"
                    detail = f"文件存在，行号 {'在范围内' if status == 'verified' else f'越界（文件 {n} 行）'}"
                except ValueError:
                    status, detail = "unverifiable", "行号无法解析"
            else:
                status, detail = "verified", "文件存在"
        else:
            status, detail = "refuted", f"文件不存在: {full}"
        results.append({"decl": m.group(0), "kind": "file", "status": status, "detail": detail})

    # 3. 无验证的符号（反引号大写/驼峰，排除工具名）
    for m in re.finditer(r"`([A-Z][A-Za-z0-9_]+)`", text):
        sym = m.group(1)
        results.append({"decl": m.group(0), "kind": "symbol", "status": "unverifiable",
                        "detail": f"符号 '{sym}' 需在代码库中检索验证"})

    verified = sum(1 for r in results if r["status"] == "verified")
    refuted = sum(1 for r in results if r["status"] == "refuted")
    unverifiable = sum(1 for r in results if r["status"] == "unverifiable")
    return {
        "total": len(results), "verified": verified, "refuted": refuted,
        "unverifiable": unverifiable,
        "结论": "存在被证伪声明（幻觉），必须纠正后才能引用" if refuted else "无被证伪声明",
        "results": results[:50],
    }
