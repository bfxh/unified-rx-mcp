# -*- coding: utf-8 -*-
"""tools/collab.py —— 协作域（2 工具）：pipeline / parallel

pipeline：工具链顺序组合（前一步输出注入下一步参数）。
parallel：并发执行（线程池）。
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from registry import tool, call

_PRESETS = {
    "audit_repo": [
        {"tool": "bug_scan", "args": {"path": "${path}"}},
        {"tool": "std_check", "args": {"path": "${path}"}},
    ],
    "guard_text": [
        {"tool": "hallucination_guard", "args": {"text": "${text}", "root": "${root}"}},
    ],
    "locate_context": [
        {"tool": "locate_edit", "args": {"path": "${path}", "query": "${query}"}},
    ],
}


def _fill(template, ctx):
    """${key} 替换。"""
    if isinstance(template, str):
        for k, v in ctx.items():
            template = template.replace("${" + k + "}", str(v))
        return template
    if isinstance(template, dict):
        return {k: _fill(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, ctx) for v in template]
    return template


@tool("pipeline", "工具链协作：顺序组合，前一步输出注入下一步", "collab",
      {"type": "object",
       "properties": {
           "preset": {"type": "string", "description": "预设配方：audit_repo/guard_text/locate_context"},
           "steps": {"type": "array", "description": "[{tool, args, as?}]——上一步结果以 ${key} 注入"},
           "path": {"type": "string"}, "text": {"type": "string"},
           "root": {"type": "string"}, "query": {"type": "string"},
       },
       "required": []})
def pipeline(preset=None, steps=None, path=None, text=None, root=None, query=None):
    ctx = {k: v for k, v in {"path": path, "text": text, "root": root,
                             "query": query}.items() if v is not None}
    if steps is None:
        if preset not in _PRESETS:
            return {"error": f"未知 preset: {preset}；可选 {list(_PRESETS)}"}
        steps = _PRESETS[preset]
    results = []
    for i, step in enumerate(steps or []):
        tool_name = step.get("tool")
        args = _fill(step.get("args") or {}, ctx)
        t0 = time.time()
        r = call(tool_name, args)
        elapsed = round((time.time() - t0) * 1000)
        results.append({"step": i, "tool": tool_name, "elapsed_ms": elapsed, "result": r})
        if not r.get("ok") and step.get("stop_on_fail"):
            break
        # 注入输出（as 别名 → ctx）
        alias = step.get("as")
        if alias and r.get("ok"):
            ctx[alias] = r.get("result")
    return {"preset": preset, "steps": len(results), "results": results}


@tool("parallel", "并发执行：多工具同时跑，全部完成后汇总（≤8 并发）", "collab",
      {"type": "object",
       "properties": {
           "tasks": {"type": "array", "description": "[{tool, args}]"},
           "timeout": {"type": "number", "description": "总超时秒（默认 60）"},
       },
       "required": ["tasks"]})
def parallel(tasks, timeout=60):
    if not tasks or len(tasks) > 8:
        return {"error": "tasks 必填，1-8 个"}
    results = [None] * len(tasks)

    def run(i, task):
        t0 = time.time()
        r = call(task.get("tool"), task.get("args") or {})
        results[i] = {"tool": task.get("tool"), "elapsed_ms": round((time.time() - t0) * 1000),
                      "result": r}

    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = [ex.submit(run, i, t) for i, t in enumerate(tasks)]
        for f in futures:
            f.result(timeout=timeout)
    return {"count": len(results), "results": results}
