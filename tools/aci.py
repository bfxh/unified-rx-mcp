# -*- coding: utf-8 -*-
"""tools/aci.py —— ACI 输出纪律层（S105，ADVANCES P1 末项）。

SWE-agent ACI 论文（NeurIPS 2024）证明**只改接口设计**值 +10.7pp：动作简单、
反馈高信号、护栏防错、错误可修复。本仓已有的（条数/字符串钳制、编辑语法门、
窗口化读取、空目录显式）不再重复，本层补三件：

1. **空结果显式说明**——"无匹配"是事实，但要说清下一步（`enrich`）；
2. **截断提示**——cursor 续读/缩小范围（registry._clamp 调用 `trunc_hint`）；
3. **错误消息可修复化**——按已知错误类补"（建议：…）"（`hint_error`）。

口径：**只在确有建议时加 `hint` 字段**，没有就不加——不制造噪音。
"""
import re

# 工具 → (结果键, 空结果提示)。只收"空 = 常见且有下一步"的工具。
_EMPTY = {
    "code_search": ("hits", "无匹配：换更短的标识符/英文关键词；确认 root 指向代码目录；"
                            "可试 hybrid=true（BM25⊕语义融合）"),
    "code_semantic": ("hits", "无匹配：自然语言短句效果更好；确认 root 正确"
                              "（首次调用会建符号向量）"),
    "locate_edit": ("hits", "无命中：换符号名或放宽 query；全库找线索用 code_search"),
    "ide_outline": ("symbols", "无符号：确认语言受支持（python/rust/go/javascript）"
                               "且文件非空"),
    "bug_scan": ("issues", "未报问题：可能确实干净，也可能是规则未覆盖该模式——"
                           "覆盖边界见 VULN-HUNTING 附录 B；不要据此判定'无漏洞'"),
    "ast_scan": ("issues", "未报结构信号：覆盖边界见 VULN-HUNTING 附录 B"),
    "fs_list": ("entries", "目录为空（或该层不可读）"),
    "repo_map": ("map", "地图为空：确认 root 下有受支持的代码文件；或放宽 max_files"),
    "dep_graph": ("graph", "无内部依赖：确认 root 下是 .py 文件（非 Python 项目不适用）"),
}

# (错误子串, 建议)。按序匹配，首个命中生效；已有建议文本不重复追加。
_ERROR_HINTS = (
    ("路径越界", "确认 UNIFIED_RX_SANDBOX 是否包含该路径（或设为 *）；也可先 fs_stat 核对"),
    ("不是目录", "先用 fs_list 确认该路径是目录"),
    ("不是文件或不存在", "先用 fs_stat 确认路径存在"),
    ("超时", "缩小范围（max_files/查询）或提高 timeout"),
    ("SchemaError", "按工具 schema 修正参数类型/必填项/枚举值"),
    ("参数错误", "检查参数名与类型是否与 schema 一致"),
    ("未检测到测试设施", "确认 path 指向含 Cargo.toml / go.mod / pytest 配置的项目根"),
)

_TRUNC_HINT_LIST = "结果被截断：用 cursor=next_cursor 续读，或缩小查询范围"
_TRUNC_HINT_STR = "字符串被截断（保头保尾）：用 fs_read 分段读取或缩小范围"
_TRUNC_HINT_NESTED = "嵌套结果被截断：缩小查询范围（该层不支持翻页）"


def _add_hint(result, hint):
    if not hint:
        return result
    old = result.get("hint")
    if old:
        if hint not in old:
            result["hint"] = f"{old}；{hint}"
    else:
        result["hint"] = hint
    return result


def enrich(name, result):
    """成功结果 → 空结果提示（原地加 `hint`，无建议时不加）。"""
    if not isinstance(result, dict):
        return result
    spec = _EMPTY.get(name)
    if not spec:
        return result
    key, hint = spec
    v = result.get(key)
    if v is None:
        return result
    if isinstance(v, (list, dict, str)) and len(v) == 0:
        return _add_hint(result, hint)
    return result


def hint_error(msg):
    """错误消息 → 追加"（建议：…）"；无对应建议或已含建议则原样返回。"""
    if not isinstance(msg, str):
        return msg
    for key, hint in _ERROR_HINTS:
        if key in msg:
            if hint in msg or "建议：" in msg:
                return msg
            return f"{msg}（建议：{hint}）"
    return msg


def trunc_hint(kind):
    """截断提示文本。kind ∈ {list, str, nested}。"""
    return {"list": _TRUNC_HINT_LIST, "str": _TRUNC_HINT_STR,
            "nested": _TRUNC_HINT_NESTED}.get(kind, "")


def strip_hint(text):
    """（测试辅助）去掉"（建议：…）"尾注（建议文本可能含嵌套括号）。"""
    idx = (text or "").rfind("（建议：")
    if idx != -1 and text.rstrip().endswith("）"):
        return text[:idx]
    return text
