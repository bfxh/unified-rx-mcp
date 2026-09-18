# -*- coding: utf-8 -*-
"""S149 契约：渐进披露（域 profile）+ sys 域提权与通用档位。

用户指令两件（本轮）：①"不需要每次把全部工具展给智能体看，部分的东西逐渐发"
→ 域级 profile：list_tools 过滤 + 越域调用清晰拒绝 + profile_enable 运行期开启
（能力变更需授权）+ list_changed 通知钩子；②"不只是游戏，工具等其他东西都要搞"
→ sys_steer 通用档位（class_/priority/eco + 名称定位）与 SeDebugPrivilege 提权路径。

本文件锁：
1. 裁剪语义：set_enabled_groups 后 list_tools 只剩该域 + 恒在工具（两把开关）；
2. 越域调用拒绝**带开启指引**（不是"未知工具"的模糊报错）；
3. profile_enable：未授权拒绝；授权后并入启用集并触发 list_changed 钩子；
4. env 解析：all/core/域列表；core 是常用开发面且**显著小于全量**；
5. sys 通用档位：class_=any 时不碰核定向；priority/eco 合法性校验；
   空档位组合报错（防"什么都没设"的静默空转）。
"""
import os

import pytest

import registry
import tools  # noqa: F401
import toolmeta


@pytest.fixture(autouse=True)
def _restore_profile():
    yield
    registry.set_enabled_groups(None)          # 每例后复位，防串味


def test_trim_filters_list_tools_but_keeps_switches():
    registry.set_enabled_groups({"fs"})
    names = {t["name"] for t in registry.list_tools()}
    assert {"fs_read", "fs_write", "fs_stat", "fs_list"} <= names
    assert "profile_status" in names and "profile_enable" in names, "两把开关恒在"
    assert "sys_topology" not in names
    assert len(names) < registry.tool_count() // 3


def test_disabled_call_gives_enable_hint():
    registry.set_enabled_groups({"fs"})
    r = registry.call("sys_topology", {})
    assert r.get("ok") is False
    assert "未启用" in r["error"] and "profile_enable" in r["error"], r["error"]


def test_profile_enable_requires_auth_and_notifies():
    registry.set_enabled_groups({"fs"})
    hits = []
    registry.set_profile_change_hook(lambda: hits.append(1))
    try:
        r = registry.call("profile_enable", {"domains": ["sys"]})
        assert r.get("ok") is False and "授权" in r["error"]
        r2 = registry.call("profile_enable", {"domains": ["sys"],
                                              "__authorized": True})
        assert r2.get("ok") and "sys" in r2["result"]["enabled_groups"]
        assert hits, "开启后必须触发 list_changed 钩子"
        assert registry.tool_enabled("sys_topology")
        bad = registry.call("profile_enable", {"domains": ["nope"],
                                               "__authorized": True})
        assert bad.get("ok") is False and "未知域" in bad["error"]
    finally:
        registry.set_profile_change_hook(None)


def test_env_profile_parsing(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_PROFILE", "all")
    assert registry.profile_from_env() is None
    monkeypatch.setenv("UNIFIED_RX_PROFILE", "core")
    core = registry.profile_from_env()
    assert core == {"fs", "scan", "ide", "search", "ops", "guard"}
    monkeypatch.setenv("UNIFIED_RX_PROFILE", "fs,sys")
    assert registry.profile_from_env() == {"fs", "sys"}
    monkeypatch.setenv("UNIFIED_RX_PROFILE", "fs,bogus")   # 未知域被过滤
    assert registry.profile_from_env() == {"fs"}


def test_core_is_much_smaller_than_full():
    full = len(registry.list_tools())
    registry.set_enabled_groups({"fs", "scan", "ide", "search", "ops", "guard"})
    core = len(registry.list_tools())
    assert core < full * 0.75, f"core 档必须显著小于全量（{core} vs {full}）"


def test_steer_generic_requires_a_knob():
    r = registry.call("sys_steer", {"target": "1", "__authorized": True})
    assert r.get("ok") is False and "至少给一个档位" in r["error"]


def test_steer_validates_knobs():
    r = registry.call("sys_steer", {"target": "1", "class_": "x",
                                    "__authorized": True})
    assert r.get("ok") is False and "class_" in r["error"]
    r2 = registry.call("sys_steer", {"target": "1", "priority": "ultra",
                                     "__authorized": True})
    assert r2.get("ok") is False and "priority" in r2["error"]


def test_new_tools_have_titles_and_annotations():
    for name in ("sys_procs", "sys_privilege", "profile_status", "profile_enable"):
        assert name in toolmeta.TOOL_TITLES, f"{name} 缺标题"
        entry = [t for t in registry.list_tools() if t["name"] == name]
        assert entry, f"{name} 未注册"
        assert isinstance(entry[0]["annotations"]["readOnlyHint"], bool)
    # 写类档位：提权/开域都是 tier ③（requires_auth）
    for name in ("sys_privilege", "profile_enable", "sys_steer"):
        assert registry._TOOLS[name]["requires_auth"] is True
