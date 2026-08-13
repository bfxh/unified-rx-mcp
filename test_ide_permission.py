"""test_ide_permission.py — IDE R2 权限分级测试（IDE_ENHANCE_PLAN R2）。

覆盖：
  1. level_of：工具→级别映射
  2. check：L1-L3 放行、L4 无授权拒绝、L4 带授权放行
  3. strip_auth：授权字段剥离
  4. 集成：fs_write 无授权拒绝 / 带授权放行（UNIFIED_RX_UNSAFE 未设时）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ide_permission as perm  # noqa: E402
import server  # noqa: E402


def test_level_mapping():
    assert perm.level_of("lsp_query") == perm.L1
    assert perm.level_of("cae_lsp_query") == perm.L1
    assert perm.level_of("cae_change_impact") == perm.L2
    assert perm.level_of("bug_locate") == perm.L3
    assert perm.level_of("fs_write") == perm.L4
    assert perm.level_of("cae_lsp_edit_merge") == perm.L4
    assert perm.level_of("unknown_tool") == perm.L1  # 未登记默认最保守


def test_check_l1_allowed():
    ok, reason = perm.check("lsp_query", {"path": "a.rs"})
    assert ok and not reason


def test_check_l4_denied_without_auth():
    ok, reason = perm.check("fs_write", {"path": "a.txt", "content": "x"})
    assert not ok
    assert "授权" in reason or "L4" in reason


def test_check_l4_allowed_with_auth():
    ok, reason = perm.check("fs_write", {"path": "a.txt", "content": "x", "__authorized": True})
    assert ok and not reason


def test_strip_auth():
    args = {"path": "a.txt", "__authorized": True}
    out = perm.strip_auth(args)
    assert "__authorized" not in out
    assert out["path"] == "a.txt"


def test_integration_fs_write_denied():
    """集成：fs_write 无授权被拒（不落盘）。"""
    if os.environ.get("UNIFIED_RX_UNSAFE") == "1":
        return  # 信任环境跳过
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_perm_test_denied.txt")
    r = server._call("fs_write", {"path": target, "content": "should not write"})
    assert r[0].text.startswith("Error: 权限拒绝"), r[0].text[:80]
    assert not os.path.exists(target), "越权写不该落盘"


def test_integration_fs_write_authorized():
    """集成：fs_write 带授权放行。"""
    if os.environ.get("UNIFIED_RX_UNSAFE") == "1":
        return
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_perm_test_ok.txt")
    r = server._call("fs_write", {"path": target, "content": "authorized", "__authorized": True})
    assert not r[0].text.startswith("Error"), r[0].text[:80]
    os.remove(target) if os.path.exists(target) else None
