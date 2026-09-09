# -*- coding: utf-8 -*-
"""S122 工具熔断契约：滑动窗口重复计数 / 空转检测 / 冷却恢复 / 旁路 / 复位。

规则：同一（工具 + 规范化参数 + cursor）在窗口内调用 **超过** limit 次 → 熔断并冷却；
同一 key 连续返回逐字节相同的结果达 limit 次 → 提前熔断（空转）。
熔断器挂在 registry.call 单一裁决点（门禁之后、缓存之前）——缓存命中的重复也算循环。
"""
import pytest

import registry
import tools  # noqa: F401
from tools import breaker


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_BREAKER", "on")
    monkeypatch.setenv("UNIFIED_RX_BREAKER_LIMIT", "3")
    monkeypatch.setenv("UNIFIED_RX_BREAKER_WINDOW_S", "300")
    monkeypatch.setenv("UNIFIED_RX_BREAKER_COOLDOWN_S", "60")
    breaker.reset()
    yield
    breaker.reset()


def test_check_trips_after_limit(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    args = {"query": "same", "root": "D:/x"}
    for _ in range(3):
        assert breaker.check("code_search", args) is None
    msg = breaker.check("code_search", args)
    assert msg and "BreakerOpen" in msg and "3" in msg, msg
    # 换参数（key 变）→ 立刻放行
    assert breaker.check("code_search", {"query": "other", "root": "D:/x"}) is None


def test_cooldown_recovers(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    for _ in range(3):
        breaker.check("bug_scan", {"path": "D:/p"})
    assert breaker.check("bug_scan", {"path": "D:/p"}) is not None
    clock.t += 61                        # 冷却到期
    assert breaker.check("bug_scan", {"path": "D:/p"}) is None


def test_window_slides(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    for _ in range(3):
        breaker.check("fs_list", {"path": "D:/w"})
    clock.t += 301                       # 全部滑出窗口（>WINDOW_S）
    assert breaker.check("fs_list", {"path": "D:/w"}) is None


def test_result_streak_trips(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    key_args = {"path": "D:/r"}
    same = {"ok": True, "result": {"files": 1}}
    for _ in range(3):
        breaker.check("fs_stat", key_args)
        breaker.record("fs_stat", key_args, same)
    msg = breaker.check("fs_stat", key_args)
    assert msg and "完全相同" in msg, msg


def test_env_bypass(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_BREAKER", "off")
    for _ in range(50):
        assert breaker.check("fs_read", {"path": "D:/b"}) is None


def test_exempt_tools_never_trip():
    for _ in range(50):
        assert breaker.check("breaker_status", {}) is None
        assert breaker.check("breaker_reset", {}) is None


def test_registry_wiring_and_reset_tools():
    """registry.call 真链路：第 4 次同参调用被拒；breaker_status/reset 可救火。"""
    args = {"path": "D:/开发/unified-rx-mcp/README.md"}
    for _ in range(3):
        r = registry.call("fs_stat", args)
        assert r.get("ok") is True, r
    r = registry.call("fs_stat", args)
    assert r.get("ok") is False and "BreakerOpen" in r["error"], r

    st = registry.call("breaker_status", {})
    assert st.get("ok"), st
    assert st["result"]["tripped"], st["result"]
    assert st["result"]["limit"] == 3, st["result"]

    rs = registry.call("breaker_reset", {})
    assert rs.get("ok") and rs["result"]["cleared_keys"] >= 1, rs
    assert registry.call("fs_stat", args).get("ok") is True

    st2 = registry.call("breaker_status", {})
    assert st2["result"]["tripped"] == [], st2["result"]


def test_snapshot_reports_busiest():
    for i in range(2):
        breaker.check("ast_scan", {"path": f"D:/s{i}"})
    breaker.check("ast_scan", {"path": "D:/s0"})
    snap = breaker.snapshot()
    assert snap["enabled"] is True and snap["limit"] == 3
    busiest = [b for b in snap["busiest"] if b["tool"] == "ast_scan"]
    assert busiest and busiest[0]["calls_in_window"] == 2, snap
