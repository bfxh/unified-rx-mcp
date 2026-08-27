# -*- coding: utf-8 -*-
"""S8 智能体自查域测试：克隆隔离 / 审计规则与掩码 / asar 自标定提取 / 清理路径安全。"""
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry   # noqa: E402
import tools.appaudit as aa  # noqa: E402

SK = "sk-aabbccddeeffgghhiijjkk00112233"
GH = "ghp_" + "A" * 34
AWS = "AKIAXW2345678901QRST"

MAIN_JS = (
    'const { shell } = require("electron");\n'
    'const cp = require("child_process");\n'
    'function boot(x){ return eval(x); }\n'
    'function go(u){ shell.openExternal(u); }\n'
    f'const k = "{SK}";\n'
    '// https://api.anthropic.com/v1/messages\n'
)

CFG_JSON = f'{{"awsKey": "{AWS}", "pat": "{GH}"}}'
# 无前缀凭据：值形状不匹配任何 sk-/AKIA 规则，只有键名能指认它（2026-08-27 实测教训）
NAMED_SECRET = '{"serviceName": "relay", "apiKey": "df11025779574a2abd2aa2aedc306fab.ZzzQQwwEE"}'


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    d = tmp_path / "audits"
    monkeypatch.setenv("UNIFIED_RX_AUDIT_SANDBOX", str(d))
    return d


@pytest.fixture
def mini_app(tmp_path):
    src = tmp_path / "mini-app"
    (src / "deep" / "node_modules" / "pkg").mkdir(parents=True)
    (src / "main.js").write_text(MAIN_JS, encoding="utf-8")
    (src / "secrets.env").write_text(f"GITHUB={GH}\nAWS={AWS}\n", encoding="utf-8")
    (src / "private.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                                     encoding="utf-8")
    (src / "urls.md").write_text("[a](https://api.anthropic.com/v1/x) [b](https://example.com/a)\n",
                                 encoding="utf-8")
    (src / "settings.json").write_text(NAMED_SECRET, encoding="utf-8")
    (src / "deep" / "node_modules" / "pkg" / "index.js").write_text('require("child_process")\n',
                                                                    encoding="utf-8")
    return src


def _clone(mini_app, sandbox):
    r = registry.call("app_clone", {"source_dir": str(mini_app)})
    assert r["ok"] is True, r
    return r["result"]


# ---------- app_clone ----------

def test_clone_happy_isolated(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    assert snap["verified"] is True
    assert snap["files"] == 6 and snap["truncated_by"] is None  # 5 文本 + node_modules 内 1 js
    # 内容级失败/元数据警告分离计数（实测大目录锁定文件暴露的错误分类契约）
    assert snap["read_fails"] == 0 and snap["meta_warns"] >= 0 and "errors" in snap
    sp = Path(snap["snapshot"])
    assert str(sp).startswith(str(sandbox))
    assert os.path.normcase(sp) != os.path.normcase(mini_app)


def test_clone_rejects_relative_and_missing(sandbox):
    assert registry.call("app_clone", {"source_dir": ""})["ok"] is False
    assert registry.call("app_clone", {"source_dir": "  \t"})["ok"] is False
    assert registry.call("app_clone", {"source_dir": "./sub"})["ok"] is False
    assert registry.call("app_clone", {"source_dir": str(Path.cwd()) + "\\no_such_dir_xyz"})["ok"] is False


def test_clone_budget_truncates(mini_app, sandbox):
    r = registry.call("app_clone", {"source_dir": str(mini_app),
                                    "max_files": 2, "max_bytes": 10 ** 12})
    s = r["result"]
    assert s["truncated_by"] == "max_files" and s["files"] == 2


def test_clone_name_derived_not_injected(mini_app, sandbox):
    s = _clone(mini_app, sandbox)
    tag = Path(s["snapshot"]).stem
    # 时间戳-hash-净化名 结构：无后缀、无原始路径字符注入面
    assert "." not in tag and "/" not in tag and "\\" not in tag
    assert tag.isascii()


# ---------- 审计边界：只许沙箱内 ----------

def test_audit_rejects_source_outside_sandbox(mini_app, sandbox):
    r = registry.call("app_audit", {"snapshot_dir": str(mini_app)})
    assert r["ok"] is False and "app_clone" in r["error"]
    assert registry.call("app_audit", {"snapshot_dir": ""})["ok"] is False


# ---------- 规则与掩码 ----------

def test_audit_findings_and_masking(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    r = registry.call("app_audit", {"snapshot_dir": snap["snapshot"], "with_asar": False})
    assert r["ok"] is True
    res = r["result"]
    labels = {(f["kind"], f["label"]) for f in res["findings"]}
    assert ("definite", "private_key_block") in labels
    assert ("clue", "eval_call") in labels and ("clue", "open_external") in labels \
        and ("clue", "child_process") in labels
    assert ("clue", "api_key_sk") in labels and ("clue", "github_pat") in labels \
        and ("clue", "aws_access_key") in labels
    # 键名指认型：无前缀凭据也必须被抓到
    assert ("clue", "secret_by_key") in labels
    dumped = json.dumps(res, ensure_ascii=False)
    for raw in (SK, GH, AWS, "-----BEGIN PRIVATE KEY-----",
                "df11025779574a2abd2aa2aedc306fab.ZzzQQwwEE"):
        assert raw not in dumped, f"原值泄漏: {raw[:10]}"
    assert dump_masked(res)


def dump_masked(res):
    return any("***len=" in f["detail"] for f in res["findings"]) or (
        pytest.fail("detail 未按掩码输出"))


def test_audit_url_inventory_and_ai_hosts(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    res = registry.call("app_audit", {"snapshot_dir": snap["snapshot"],
                                      "with_asar": False})["result"]
    hosts = {u["host"] for u in res["url_host_top"]}
    assert "api.anthropic.com" in hosts and "example.com" in hosts
    assert "api.anthropic.com" in res["ai_endpoint_hosts"]


# ---------- asar：合成最小包验证自标定提取 ----------

def _synth_asar(path: Path, entries):
    """entries: {rel: bytes}。模拟实证观测到的真实布局：
    4×u32 前导 + JSON 头（字符从 b'{\"files\"' 起）+ 对齐 + 内容区。"""
    pos = 0
    tree = {"files": {}}
    payload = []
    for rel, data in entries.items():
        h = hashlib.sha256(data).hexdigest()
        tree["files"][rel] = {"size": len(data), "offset": str(pos),
                              "integrity": {"algorithm": "SHA256", "hash": h}}
        payload.append(data)
        pos += len(data)
    head = json.dumps(tree).encode()
    pre = struct.pack("<4I", 4, len(head) + 8, len(head) + 4, len(head))
    path.write_bytes(pre + head + b"\x00\x00\x00\x00" + b"".join(payload))


def test_asar_extraction_calibrated_and_rescanned(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    apath = Path(snap["snapshot"]) / "resources" / "app.asar"
    apath.parent.mkdir(parents=True, exist_ok=True)
    _synth_asar(apath, {"main.js": MAIN_JS.encode(), "cfg.json": CFG_JSON.encode()})

    res = registry.call("app_audit", {"snapshot_dir": snap["snapshot"]})["result"]
    stat = next(a for a in res["asar"] if a["asar"].endswith("app.asar"))
    assert stat.get("extracted") == 2 and "error" not in stat, stat
    labels = {f["label"] for f in res["findings"]}
    assert "asar:eval_call" in labels and "asar:api_key_sk" in labels \
        and "asar:aws_access_key" in labels
    dumped = json.dumps(res, ensure_ascii=False)
    assert AWS not in dumped and SK not in dumped


def test_asar_corrupt_reports_error_not_crash(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    bad = Path(snap["snapshot"]) / "broken.asar"
    bad.write_bytes(b"not an asar at all" * 100)
    res = registry.call("app_audit", {"snapshot_dir": snap["snapshot"],
                                      "with_asar": True})["result"]
    st = next(a for a in res["asar"] if a["asar"].endswith("broken.asar"))
    assert "error" in st


# ---------- 清理：授权 + 路径安全 ----------

def test_clean_requires_auth(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    r = registry.call("app_clean", {"target": snap["snapshot"]})
    assert r["ok"] is False and "授权" in r["error"]
    assert Path(snap["snapshot"]).exists()


def test_clean_removes_clone_authorized(mini_app, sandbox):
    snap = _clone(mini_app, sandbox)
    p = Path(snap["snapshot"])
    r = registry.call("app_clean", {"target": snap["snapshot"], "__authorized": True})
    assert r["ok"] is True and r["result"]["removed"] is True
    assert not p.exists()


def test_clean_refuses_outside_and_root(mini_app, sandbox):
    args = {"__authorized": True}
    assert registry.call("app_clean", {"target": str(mini_app), **args})["ok"] is False
    assert registry.call("app_clean", {"target": str(sandbox), **args})["ok"] is False
    assert registry.call("app_clean", {"target": "", **args})["ok"] is False
    assert registry.call("app_clean",
                         {"target": str(mini_app) + "\\..\\..", **args})["ok"] is False
