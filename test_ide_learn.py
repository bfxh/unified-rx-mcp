"""test_ide_learn.py — 记忆维深化测试（2026-08-13，lesson_learn 工具 + lse-engine 修复）。

覆盖：
  1. lesson_learn store/state/recall（分层教训存取）
  2. lesson_learn ucb_select/ucb_backprop（UCB 学习闭环——引擎空格解析修复回归）
  3. lesson_learn delta（经验得分更新）
"""
import json
import os
import sys
import tempfile

# 测试隔离：LSE 状态重定向到临时路径（避免污染 ~/.unified-rx/lse-state.json
# 及与 test_unified_rx 的 lesson 测试并发冲突）
_ISOLATED_STATE = os.path.join(tempfile.mkdtemp(prefix="lse-test-"), "lse-state.json")
os.environ["LSE_STATE"] = _ISOLATED_STATE

sys.path.insert(0, sys.path[0])

import server  # noqa: E402


def test_lesson_store_and_state():
    r = server._call("lesson_learn", {"action": "store", "tier": "work",
                                      "content": "测试教训：RRF 融合优于单路检索"})
    d = json.loads(r[0].text)
    assert d.get("ok") is True, d.get("error", "")
    lid = d.get("result", {}).get("id", "")
    assert lid.startswith("work_")
    r2 = server._call("lesson_learn", {"action": "state"})
    d2 = json.loads(r2[0].text)
    assert d2.get("ok") is True
    assert lid in d2.get("result", {}).get("lessons", {})


def test_lesson_recall():
    r = server._call("lesson_learn", {"action": "store", "tier": "work",
                                      "content": "可召回教训：AABB 碰撞用中心距离"})
    d = json.loads(r[0].text)
    lid = d.get("result", {}).get("id", "")
    r2 = server._call("lesson_learn", {"action": "recall", "lesson_id": lid})
    d2 = json.loads(r2[0].text)
    assert d2.get("ok") is True


def test_ucb_select_fixed():
    """lse-engine 空格解析修复回归：ucb_select 不再报 no children。"""
    r = server._call("lesson_learn", {"action": "ucb_select",
                                      "parent": "root", "children": ["a", "b", "c"]})
    d = json.loads(r[0].text)
    assert d.get("ok") is True, f"引擎空格解析未修复: {d.get('error')}"
    assert d.get("result", {}).get("selected") in ("a", "b", "c")


def test_ucb_backprop():
    r = server._call("lesson_learn", {"action": "ucb_backprop", "node_id": "a", "reward": 1.0})
    d = json.loads(r[0].text)
    assert d.get("ok") is True


def test_lesson_delta():
    r = server._call("lesson_learn", {"action": "store", "tier": "learn",
                                      "content": "delta 测试教训"})
    d = json.loads(r[0].text)
    lid = d.get("result", {}).get("id", "")
    r2 = server._call("lesson_learn", {"action": "delta", "id": lid, "delta": 0.3})
    d2 = json.loads(r2[0].text)
    assert d2.get("ok") is True
