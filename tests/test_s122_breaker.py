"""S122 工具熔断契约：滑动窗口重复计数 / 空转检测 / 冷却恢复 / 旁路 / 复位。

规则：同一（工具 + 规范化参数 + cursor）在窗口内调用 **超过** limit 次 → 熔断并冷却；
同一 key 连续返回逐字节相同的结果达 limit 次 → 提前熔断（空转）。
熔断器挂在 registry.call 单一裁决点（门禁之后、缓存之前）——缓存命中的重复也算循环。
"""
import os

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
    # S140：默认把全局护栏抬到不干扰 per-key 用例的水平，专门的全局用例再自行调低
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "1000000")
    monkeypatch.setenv("UNIFIED_RX_DAILY_ALERT", "0")
    breaker.reset()
    breaker._DAILY.update(day=None, count=0, alerted=False)
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
    """registry.call 真链路：第 4 次同参调用被拒；breaker_status/reset 可救火。

    S125 修：路径改自仓库根推导（原为写死本机路径——CI 上沙盒外第一调即败，
    熔断计数到不了阈值；本机路径硬编码是 S122 漏网的可移植性缺陷）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args = {"path": os.path.join(root, "README.md").replace(os.sep, "/")}
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


# ---- S140 全局护栏：QPM 熔断 / 每日告警 / 豁免与复位语义 ----


def test_global_qpm_trips_on_varied_args(monkeypatch):
    """变参穷举（per-key 永不重复）也要被全局闸拦住——S140 的核心场景。"""
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "4")
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_COOLDOWN_S", "60")
    breaker.reset()
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    for i in range(4):
        assert breaker.check("fs_read", {"path": f"D:/f{i}"}) is None
    msg = breaker.check("fs_read", {"path": "D:/f9"})
    assert msg and "全局熔断" in msg, msg
    clock.t += 61                          # 冷却到期自动恢复
    assert breaker.check("fs_read", {"path": "D:/f10"}) is None


def test_global_qpm_off(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "0")
    breaker.reset()
    for i in range(30):
        assert breaker.check("fs_read", {"path": f"D:/g{i}"}) is None


def test_exempt_tools_bypass_global_trip(monkeypatch):
    """全局熔断期间救火通道（breaker_status/reset）必须仍开着。"""
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "1")
    breaker.reset()
    assert breaker.check("fs_read", {"path": "D:/a"}) is None
    assert breaker.check("fs_read", {"path": "D:/b"}) is not None
    assert breaker.check("breaker_status", {}) is None
    assert breaker.check("breaker_reset", {}) is None


def test_reset_clears_global_keeps_daily(monkeypatch):
    """reset 清全局熔断态，但日总量计量保留（防止复位后被再次拉爆绕过告警）；
    被全局闸拒绝的调用不计入——它没执行，不是消耗。"""
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "1")
    breaker.reset()
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    assert breaker.check("fs_read", {"path": "D:/a"}) is None       # 通过并计数
    msg = breaker.check("fs_read", {"path": "D:/b"})                # 触发全局熔断并被拒
    assert msg and "全局熔断" in msg, msg
    breaker.reset()
    snap = breaker.snapshot()
    assert snap["global"]["tripped"] is False, snap["global"]
    assert snap["global"]["daily_calls"] == 1, snap["global"]


def test_daily_alert_fires_once(monkeypatch, tmp_path):
    alarm_file = tmp_path / "alarms.jsonl"
    monkeypatch.setattr(breaker, "_alarm_file", lambda: str(alarm_file))
    monkeypatch.setenv("UNIFIED_RX_DAILY_ALERT", "3")
    breaker.reset()
    clock = _Clock()
    monkeypatch.setattr(breaker, "_now", clock)
    for i in range(5):
        assert breaker.check("fs_read", {"path": f"D:/d{i}"}) is None
    lines = alarm_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, lines                   # 只告警一次，不刷屏
    assert "daily_calls" in lines[0] and "3" in lines[0]


def test_snapshot_reports_global_state(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_GLOBAL_QPM", "600")
    monkeypatch.setenv("UNIFIED_RX_DAILY_ALERT", "50000")
    breaker.reset()
    breaker._DAILY.update(day=None, count=7, alerted=False)
    snap = breaker.snapshot()
    g = snap["global"]
    assert g["qpm"] == 600 and g["tripped"] is False, g
    assert g["daily_calls"] == 7 and g["daily_alert_at"] == 50000, g
