#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""multi_agent.py — 多智能体编排（抄 crewAI/autogen 角色分工 + 并行协作）。

不是真 LLM 智能体（那要接模型），而是**工具编排层**：
把任务按角色拆解 → 每角色分配工具子集 → 并行执行 → 汇总。

角色（role → 工具集）：
  - analyst  分析者：repo_graph/kb_query/bug_scan（看懂代码）
  - quality  质检员：quality_scan/std_check（找问题）
  - memory   记忆员：lesson_recall_lse/lesson_extract/lesson_feedback（经验）
  - writer   写手：fs_read/fs_write（读写文件）
  - explorer 探索者：explore_engine/bug_locate（深挖）

用法：
  orchestrate(tasks, roles) -> dict   # tasks: [{id, role, tool, args}]
  并行跑同角色任务，串行跑跨角色（保持依赖简单）。
"""
import concurrent.futures
import time

# 角色 → 允许的工具集合（白名单）
ROLE_TOOLS: dict[str, set] = {
    "analyst": {"repo_graph", "kb_query", "bug_scan", "code_complete", "locate_edit"},
    "quality": {"quality_scan", "std_check", "ui_check", "bug_scan"},
    "memory": {"lesson_recall_lse", "lesson_extract", "lesson_feedback",
               "rule_feedback", "bug_locate_feedback"},
    "writer": {"fs_read", "fs_write", "fs_stat"},
    "explorer": {"bug_locate", "repo_graph", "kb_query"},
}


def _validate_task(task: dict) -> None:
    """校验任务结构（角色存在 + 工具属于该角色）。"""
    role = task.get("role")
    if role not in ROLE_TOOLS:
        raise ValueError(f"未知角色: {role}（可选 {sorted(ROLE_TOOLS)}）")
    tool = task.get("tool")
    if tool not in ROLE_TOOLS[role]:
        raise ValueError(f"工具 {tool} 不属于角色 {role}（允许 {sorted(ROLE_TOOLS[role])}）")


def orchestrate(tasks: list[dict], call_fn, timeout: float = 120.0,
                max_workers: int = 4) -> dict:
    """多智能体编排主入口。

    tasks:    [{id, role, tool, args}]——每个任务是一个"智能体动作"
    call_fn: (tool, args) -> result  底层工具调用器（server._call 或 mock）
    timeout: 单任务超时
    max_workers: 并行度
    返回 {ok, results: {task_id: result}, stats}
    """
    t0 = time.perf_counter()
    results: dict = {}
    errors: dict = {}
    # 校验在任务级捕获（越权/未知角色 → 该任务失败，不炸整体）
    def validate(task: dict) -> str | None:
        role = task.get("role")
        if role not in ROLE_TOOLS:
            return f"未知角色: {role}（可选 {sorted(ROLE_TOOLS)}）"
        tool = task.get("tool")
        if tool not in ROLE_TOOLS[role]:
            return f"工具 {tool} 不属于角色 {role}（允许 {sorted(ROLE_TOOLS[role])}）"
        return None

    def run(task: dict):
        v_err = validate(task)
        if v_err:
            return task["id"], {"ok": False, "tool": task.get("tool", ""),
                                "error": v_err}
        try:
            r = call_fn(task["tool"], task.get("args") or {})
            return task["id"], {"ok": True, "tool": task["tool"], "result": r}
        except Exception as exc:
            return task["id"], {"ok": False, "tool": task["tool"], "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(tasks) or 1)) as ex:
        futs = {ex.submit(run, t): t["id"] for t in tasks}
        for fut in concurrent.futures.as_completed(futs):
            tid, res = fut.result()
            if res["ok"]:
                results[tid] = res
            else:
                errors[tid] = res

    return {"ok": len(errors) == 0, "results": results, "errors": errors,
            "stats": {"tasks": len(tasks), "succeeded": len(results),
                      "failed": len(errors),
                      "ms": round((time.perf_counter() - t0) * 1000, 1)}}


def role_catalog() -> dict:
    """角色目录（给 AI 看：角色→工具→用途）。"""
    return {role: {"tools": sorted(tools),
                   "desc": _ROLE_DESC.get(role, "")}
            for role, tools in ROLE_TOOLS.items()}


_ROLE_DESC = {
    "analyst": "代码分析者：图查询/语义检索/漏洞扫描，看懂代码结构",
    "quality": "质检员：多后端质量扫描/标准检查，找出问题",
    "memory": "记忆员：教训召回/提取/反馈，维护经验库",
    "writer": "写手：安全读写文件",
    "explorer": "探索者：树搜索定位 bug，深挖根因",
}
