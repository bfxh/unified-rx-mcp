# -*- coding: utf-8 -*-
"""S77（VULN-HUNTING P0-a）：auth_gate_sweep 授权门自审的回归测试。

S75 靠人眼盘点出 4 个实锤，本工具把方法固化：漏一处门即 ok:False。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
import tools  # noqa: E402,F401  注册全部


def test_sweep_all_clean():
    r = registry.call("auth_gate_sweep", {})
    assert r["ok"] is True, r
    res = r["result"]
    assert res["ok"] is True, f"门审计发现缺口: {res}"
    assert res["漏拒绝"] == [] and res["漏声明"] == [] and res["门参数未强制"] == [], res
    assert res["manifest一致性"] == "pass", res
    assert res["总工具数"] == len(registry.list_tools())
    assert res["挂门数"] >= 17, f"S73/S75 已挂门的工具不应凭空减少: {res['挂门数']}"
    # S77：ide_lsp 混合读写（读开放 + rename_apply handler 内自查）声明手动门
    assert res["手动门"] == ["ide_lsp"], res["手动门"]


def test_sweep_gated_list_contains_known():
    r = registry.call("auth_gate_sweep", {})
    gated = set(r["result"]["挂门清单"])
    # S73/S75 逐个手工挂的门，自审必须全部看见
    for name in ("blender_verify", "process", "backup", "code_coverage",
                 "app_clone", "fs_write", "local_run", "ide_edit_multi"):
        assert name in gated, f"{name} 应在挂门清单: {gated}"


def test_gate_report_catches_bad_registrations():
    """纯函数注入坏样本：三种坏注册都必须被抓出来，手动门单独归类。"""
    from tools.attack import _gate_report
    gated, declared_missing, forced_missing, manual = _gate_report([
        ("good_tool", True, True, True, False),      # 门齐全
        ("no_declare_tool", True, False, False, False),  # 挂门但 schema 未声明（S72b 契约破）
        ("fake_gate_tool", False, True, False, False),   # 收 __authorized 无任何声明——假门
        ("manual_tool", False, True, False, True),   # 声明手动门（混合读写）——单独归类
        ("open_tool", False, False, False, False),   # 无门工具（正常，不报）
    ])
    assert gated == ["good_tool", "no_declare_tool"]
    assert declared_missing == ["no_declare_tool"]
    assert forced_missing == ["fake_gate_tool"]
    assert manual == ["manual_tool"]


def test_sweep_manifest_high_privilege_matches():
    """manifest"高权限"段（S75）与自审挂门清单是同一事实的两个投影，必须一致。"""
    sweep = registry.call("auth_gate_sweep", {})["result"]
    manifest = registry.call("capability_manifest", {})["result"]
    assert set(sweep["挂门清单"]) == set(manifest["高权限"]["工具"])
