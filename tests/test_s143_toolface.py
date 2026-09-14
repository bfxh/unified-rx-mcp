# -*- coding: utf-8 -*-
"""S143 契约：工具面注解（annotations/title）+ 体量仪表门。

背景（EXTERNAL-ALIGNMENT A1/A2，2026-09-14 联网对标轮产出）：annotations 是
MCP 规范 2025-03-26（我们钉的版本）即有的字段，此前一直漏发；tools/list 是
会话开场的固定摊派，此前无仪表、只会无声膨胀。

四锁：
1. 注解全覆盖——每个工具都有非空 annotations.title，且 toolmeta 与注册表
   **双向一致**（改名/退役工具不同步即红）；
2. 注解映射授权三档（HARDENING §七 同口径）——① ② 档（不挂门）
   readOnlyHint + idempotentHint；③ 档（requires_auth）readOnlyHint=false +
   destructiveHint=true（显式写出，不押注宿主实现规范默认值）；
3. 体量仪表真跑绿——剥掉沙盒 env 自给自足（同 secrets 门 S134 纪律）；
4. 体量门是真门——抬帽 env 压到 1 必须 FAIL（不是"永远绿"的假门）。
"""
import os
import subprocess
import sys

import registry
import tools  # noqa: F401
import toolmeta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "toolface_budget.py")


def _run_budget(extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "UNIFIED_RX_SANDBOX"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, shell=False)


def test_annotations_title_full_coverage_and_bidirectional():
    live = set(registry._TOOLS)
    assert set(toolmeta.TOOL_TITLES) == live, (
        f"toolmeta 与注册表不一致: 缺 {live - set(toolmeta.TOOL_TITLES)}, "
        f"陈旧 {set(toolmeta.TOOL_TITLES) - live}")
    titles = []
    for t in registry.list_tools():
        ann = t.get("annotations") or {}
        title = ann.get("title")
        assert isinstance(title, str) and title.strip(), f"{t['name']} 缺 title"
        titles.append(title)
    assert len(set(titles)) == len(titles), "标题必须唯一（宿主靠它区分工具）"


def test_annotations_mirror_auth_tiers():
    for t in registry.list_tools():
        ann = t.get("annotations") or {}
        if registry._TOOLS[t["name"]].get("requires_auth"):
            assert ann.get("readOnlyHint") is False, t["name"]
            assert ann.get("destructiveHint") is True, t["name"]
        else:
            assert ann.get("readOnlyHint") is True, t["name"]
            assert ann.get("idempotentHint") is True, t["name"]


def test_toolface_budget_gate_runs_green():
    cp = _run_budget()
    assert cp.returncode == 0, f"体量门红: {cp.stdout}\n{cp.stderr}"
    assert "TOOLFACE-GATE OK" in cp.stdout, cp.stdout
    assert "total_chars=" in cp.stdout and "est_tokens=" in cp.stdout, cp.stdout


def test_toolface_budget_gate_is_a_real_gate():
    cp = _run_budget({"UNIFIED_RX_TOOLFACE_CAP": "1"})
    assert cp.returncode != 0, "压到 1 字符还绿——这是假门"
    assert "TOOLFACE-GATE FAIL" in (cp.stdout + cp.stderr)
