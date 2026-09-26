"""cargo_audit（Rust 依赖安全审计薄壳）契约测试。

口径（spec 见模块 docstring）：
- 能力探测：cargo-audit 缺失如实 available=False + 安装 hint（不静默、不假绿）；
- 沙盒：路径过 _fs_resolve，越界报错；目录缺 Cargo.lock 显式报错；
- 解析纯函数 _summarize：防御式抽取（vulnerabilities.list / count / warnings 各形态），
  截断上限生效；
- 授权：requires_auth（执行外部子进程），registry 层统一把关（auth_gate_sweep 全量扫）。
真机验证（本机 cargo-audit 0.22.2 + RustSec advisory-db）：零依赖 crate
（unified-rx-mcp rust/）→ 0 漏洞 0 警告 exit 0；--stale 离线路径实测可用
（advisory DB 手动克隆到 ~/.cargo/advisory-db 后跳过更新）。
"""
import json

from tools.cargoaudit import _summarize

_PAYLOAD_CLEAN = {
    "vulnerabilities": {"list": [], "count": 0},
    "warnings": {},
}

_PAYLOAD_VULNS = {
    "vulnerabilities": {
        "count": 2,
        "list": [
            {"id": "RUSTSEC-2021-0001", "package": {"name": "ammonia", "version": "3.1.0"},
             "title": "Uncontrolled recursion", "patched": [">=3.1.1"]},
            {"id": "RUSTSEC-2020-9999", "package": {"name": "legacy", "version": "0.1.0"},
             "title": "RCE", "patched": []},
        ],
    },
    "warnings": {
        "unmaintained": [
            {"package": {"name": "old-crate", "version": "1.0.0"}},
            {"package": {"name": "old-crate2", "version": "2.0.0"}},
        ],
    },
}


def test_summarize_clean():
    out = _summarize(_PAYLOAD_CLEAN, 50)
    assert out["vulnerability_count"] == 0
    assert out["vulnerabilities"] == []
    assert out["warnings"] == {}


def test_summarize_extracts_fields():
    out = _summarize(_PAYLOAD_VULNS, 50)
    assert out["vulnerability_count"] == 2
    assert out["vulnerabilities"][0]["id"] == "RUSTSEC-2021-0001"
    assert out["vulnerabilities"][0]["package"] == "ammonia"
    assert out["vulnerabilities"][0]["patched"] == [">=3.1.1"]
    assert out["vulnerabilities"][1]["patched"] == []
    assert out["warnings"]["unmaintained"]["count"] == 2
    assert "old-crate" in out["warnings"]["unmaintained"]["packages"]


def test_summarize_truncates():
    payload = {"vulnerabilities": {"count": 5, "list": [
        {"id": f"RUSTSEC-{i}", "package": {"name": f"p{i}", "version": "1"},
         "title": "", "patched": []} for i in range(5)]}, "warnings": {}}
    out = _summarize(payload, 3)
    assert len(out["vulnerabilities"]) == 3
    assert out["vulnerability_count"] == 5


def test_summarize_defensive_on_odd_shapes():
    # cargo-audit 各版本 JSON 形状有差异：缺键一律如实置空，不抛
    assert _summarize({}, 50)["vulnerability_count"] == 0
    partial = {"vulnerabilities": {"list": [{"id": "X"}], "count": 1}}
    out = _summarize(partial, 50)
    assert out["vulnerabilities"][0]["package"] == ""
    assert out["vulnerabilities"][0]["title"] == ""


# ---- 能力探测与沙盒（工具整体回路；CI 无 cargo-audit 也走得到）----


def test_unavailable_when_binary_missing(tmp_path, monkeypatch):
    import tools.cargoaudit as m
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    out = m.cargo_audit(str(tmp_path))
    assert out["available"] is False
    assert "cargo install cargo-audit" in out["hint"]


def test_sandbox_rejects_outside(tmp_path, monkeypatch):
    import tools.cargoaudit as m
    monkeypatch.setattr(m.shutil, "which", lambda name: "cargo-audit")
    out = m.cargo_audit("Z:/definitely/outside")
    assert "error" in out and "沙盒" in out["error"]


def test_missing_lockfile_is_explicit(tmp_path, monkeypatch):
    import tools.cargoaudit as m
    monkeypatch.setattr(m.shutil, "which", lambda name: "cargo-audit")
    out = m.cargo_audit(str(tmp_path))
    assert "Cargo.lock" in out["error"]


def test_requires_auth_declared():
    from registry import _TOOLS
    assert _TOOLS["cargo_audit"].get("requires_auth") is True


def test_json_parsing_tolerates_empty_stdout():
    # cargo-audit 异常退出时 stdout 可能为空——工具层已在 exit code 拦截，
    # 这里只锁 _summarize 对空 payload 的防御
    assert _summarize(json.loads("{}"), 50)["vulnerability_count"] == 0
