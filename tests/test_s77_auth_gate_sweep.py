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
    assert (res["deny_missing"] == [] and res["declared_missing"] == []
            and res["forced_missing"] == []), res
    assert res["manifest_consistency"] == "pass", res
    assert res["total_tools"] == len(registry.list_tools())
    assert res["gated_count"] >= 17, f"S73/S75 已挂门的工具不应凭空减少: {res['gated_count']}"
    # S77：ide_lsp 混合读写（读开放 + rename_apply handler 内自查）声明手动门
    assert res["manual_gate"] == ["ide_lsp"], res["manual_gate"]


def test_sweep_gated_list_contains_known():
    r = registry.call("auth_gate_sweep", {})
    gated = set(r["result"]["gated"])
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
    assert set(sweep["gated"]) == set(manifest["高权限"]["工具"])


# ---------- S132（DESIGN-REVIEW H3）：组合透传静态自审 ----------

def test_passthrough_scanner_red_and_green():
    """纯函数双侧：缺 __authorized → 报；带（含括号嵌套/多行 dict）→ 不报。"""
    from tools.attack import _scan_compose_passthrough
    gated = {"gated_x", "gated_y"}
    src = (
        'registry.call("gated_x", {"p": 1})\n'                    # 缺 → 报
        'call("open_tool", {"p": 1})\n'                           # 非挂门 → 不报
        'registry.call("gated_y", {\n'
        '    "p": f({"k": 1}),\n'
        '    "__authorized": True,\n'                             # 带 → 不报
        '})\n'
        'x_name = "gated_x"\n'
        'registry.call(x_name, {})\n'                             # 变量名=动态面不判
    )
    hits = _scan_compose_passthrough([("t.py", src)], gated)
    assert hits == ["t.py:1 → gated_x"], hits


def test_passthrough_real_repo_is_clean():
    """真机自审：本仓 tools/ + bench/ 无漏透传（S130/swe_repair 病灶至此有常驻检查器）。"""
    from tools.attack import _compose_passthrough_scan
    assert _compose_passthrough_scan() == []


def test_sweep_reports_passthrough_key():
    r = registry.call("auth_gate_sweep", {})
    assert r["ok"] is True, r
    assert r["result"]["compose_passthrough"] == "pass", r["result"]
