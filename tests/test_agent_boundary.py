"""S173：智能体边界门控（按身份的 deny 档）——用户"能清楚其他智能体搞项目的边界"的机器化。

契约（都含"真门"负例）：
1. 配置 deny ⇒ 该 agent 调用被拒（error 带 BoundaryError + 配置 reason），**授权无法越过**；
2. 非 deny 工具/其他 agent/无配置 ⇒ 照常（向后兼容：无配置 = 如实无门）；
3. mtime 热重载：配置改了不用重启；坏 JSON ⇒ 如实当无门（不炸调用面）。
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402


@pytest.fixture
def boundary_env(tmp_path, monkeypatch):
    cfg = tmp_path / "agent-boundaries.json"
    monkeypatch.setenv("UNIFIED_RX_AGENT_BOUNDARIES", str(cfg))
    monkeypatch.setattr(registry, "_BOUNDARY_CACHE", {"mtime": None, "agents": None})
    return cfg


def _deny_cfg(cfg, agent, tools, reason):
    cfg.write_text(json.dumps(
        {"_doc": "t", "agents": {agent: {"deny": tools, "reason": reason}}},
        ensure_ascii=False), encoding="utf-8")
    # mtime 粒度可能秒级——直接失效缓存
    registry._BOUNDARY_CACHE.update(mtime=None, agents=None)


def test_boundary_denies_listed_tool(boundary_env):
    cfg = boundary_env
    _deny_cfg(cfg, "Strict Agent", ["fs_write"], "只读观察者：禁止写面")
    registry.set_agent("Strict Agent")
    try:
        r = registry.call("fs_write", {"path": str(boundary_env), "content": "x",
                                       "__authorized": True})
        assert r["ok"] is False
        assert "BoundaryError" in r["error"], r
        assert "只读观察者" in r["error"], r
    finally:
        registry.set_agent(None)


def test_boundary_is_identity_layer_not_defeated_by_auth(boundary_env):
    """授权（__authorized: true）**不能越过边界**——身份层先于授权门。"""
    cfg = boundary_env
    _deny_cfg(cfg, "Limited", ["fs_write"], "写面整域禁用")
    registry.set_agent("Limited")
    try:
        r = registry.call("fs_write", {"path": str(cfg), "content": "x",
                                       "__authorized": True})
        assert r["ok"] is False and "BoundaryError" in r["error"], r
    finally:
        registry.set_agent(None)


def test_boundary_other_agent_and_tools_unaffected(boundary_env):
    cfg = boundary_env
    _deny_cfg(cfg, "Limited", ["fs_write"], "仅 Limited")
    registry.set_agent("Other Agent")
    try:
        # 其他 agent：同一工具放行（读侧工具）
        r = registry.call("fs_read", {"path": str(cfg)})
        assert r["ok"] is True, r
    finally:
        registry.set_agent(None)
    # 同 agent 非 deny 工具也放行
    registry.set_agent("Limited")
    try:
        r = registry.call("fs_read", {"path": str(cfg)})
        assert r["ok"] is True, r
    finally:
        registry.set_agent(None)


def test_boundary_absent_config_allows_all(boundary_env):
    """无配置文件 = 如实无门（向后兼容）。fs_read 指向一个真实存在的文件。"""
    probe = boundary_env.parent / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    registry.set_agent("Anyone")
    try:
        r = registry.call("fs_read", {"path": str(probe)})
        assert r["ok"] is True, r
    finally:
        registry.set_agent(None)


def test_boundary_bad_json_fails_open_but_honest(boundary_env):
    """坏 JSON ⇒ 当无门（不炸调用面）；修好即生效（mtime 重读）。"""
    cfg = boundary_env
    cfg.write_text("{broken", encoding="utf-8")
    registry._BOUNDARY_CACHE.update(mtime=None, agents=None)
    registry.set_agent("X")
    try:
        r = registry.call("fs_read", {"path": str(cfg)})
        assert r["ok"] is True, r
    finally:
        registry.set_agent(None)
    _deny_cfg(cfg, "X", ["fs_read"], "修好后应生效")
    registry.set_agent("X")
    try:
        r = registry.call("fs_read", {"path": str(cfg)})
        assert r["ok"] is False and "BoundaryError" in r["error"], r
    finally:
        registry.set_agent(None)
