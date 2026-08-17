#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_permission.py — IDE 权限分级 L1-L4（IDE_ENHANCE_PLAN R2，抄 AetherStudio L1-L4）。

L1 只读查询（诊断/符号/悬停——默认，无限制）
L2 只读深查（引用链/影响面——可调 repo_graph/change_impact）
L3 建议修改（locate_edit/bug 修复方案——不落盘）
L4 写操作（fs_write/lsp_edit_merge 等——需显式授权）

授权机制：L4 工具调用必须带 "__authorized": true（显式授权），否则拒绝。
环境变量 UNIFIED_RX_UNSAFE=1 可全局关闭检查（CI/本地信任环境）。
"""

import os

# 权限级别
L1 = 1  # 只读查询
L2 = 2  # 只读深查
L3 = 3  # 建议修改（不落盘）
L4 = 4  # 写操作（需授权）

# 工具名（含 cae_ 前缀）→ 权限级别
TOOL_LEVELS: dict[str, int] = {
    # L1 只读查询
    "lsp_query": L1,
    "cae_lsp_query": L1,
    "cae_lsp_semantic_tokens_decode": L1,
    "cae_lsp_position_convert": L1,
    "cae_aether_goto_parse": L1,
    "cae_aether_lang_support": L1,
    "cae_aether_probe": L1,
    "cae_aether_model_provider": L1,
    "cae_file_dedup_state": L1,
    "cae_code_context": L1,
    "code_complete": L1,
    "fs_read": L1,
    "fs_stat": L1,
    "fs_list": L1,
    "kb_query": L1,
    "repo_graph": L1,
    "repo_wiki": L1,
    "bug_scan": L1,
    "quality_scan": L1,
    # L2 只读深查
    "cae_change_impact": L2,
    "cae_lesson_recall": L2,
    "change_impact": L2,
    "locate_edit": L2,
    "lesson_recall_lse": L2,
    "lesson_extract": L2,
    # L3 建议修改（不落盘）
    "bug_locate": L3,
    "bug_locate_feedback": L3,
    "rule_feedback": L3,
    # L4 写操作（需授权）
    "fs_write": L4,
    "cae_lsp_edit_merge": L4,
    "cae_aether_agent_parse": L4,  # 解析出编辑指令 → 视为写入口
    # 2026-08-15（security-review HIGH）：bug_bisect execute=true 会
    # git checkout 改写工作区 + 跑任意 test_cmd——L4 授权（未登记默认
    # L1 只读——execute 路径将绕过授权模型）
    "bug_bisect": L4,
    # 2026-08-17（security-review CRITICAL 回归）：git_bisect_find 与
    # bug_bisect 同模式（git checkout + test_cmd）——必须 L4；同轮新增的
    # 写/执行类工具一并登记（scan 系读只读但含 subprocess——L2 保守；
    # train_export 写文件 / local_run 执行脚本——L4）
    "git_bisect_find": L4,
    "train_export": L4,
    "local_run": L4,
    "local_tools": L4,
    "scan_now": L2,
    "scan_delta": L2,
    "scan_all": L2,
}

# 授权字段名（L4 工具 args 中必须为 true）
AUTH_FIELD = "__authorized"


def level_of(tool_name: str) -> int:
    """工具权限级别（未登记默认 L1 最保守——只读）。"""
    return TOOL_LEVELS.get(tool_name, L1)


def check(tool_name: str, args: dict | None) -> tuple[bool, str]:
    """权限检查：返回 (是否放行, 拒绝原因)。

    L1-L3 放行（L3 只建议不落盘）；L4 需显式授权字段。
    """
    args = args or {}
    if os.environ.get("UNIFIED_RX_UNSAFE") == "1":
        return True, ""
    lvl = level_of(tool_name)
    if lvl < L4:
        return True, ""
    if args.get(AUTH_FIELD) is True:
        return True, ""
    return False, (
        f"权限拒绝（L4 写操作）：{tool_name} 需要显式授权。"
        f"在参数中加 \"{AUTH_FIELD}\": true 确认后重试。"
    )


def strip_auth(args: dict) -> dict:
    """剥离授权字段（不让它进工具实现——防污染）。"""
    out = dict(args)
    out.pop(AUTH_FIELD, None)
    return out
