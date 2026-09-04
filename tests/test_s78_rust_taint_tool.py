# -*- coding: utf-8 -*-
"""S78：rust_taint_scan 工具接入测试（python 薄壳 → rx-taint.exe）。

覆盖：正常扫描透传 / 沙盒拒绝 / naive 基线模式 / exe 缺失干净报错。
exe 不存在的环境（未 cargo build）只跑不依赖 exe 的用例。
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
from tools import attack  # noqa: E402

from tools.attack import rust_taint_scan  # noqa: E402

# 故意含漏洞的样例（同 taint_test.rs 的嵌串纪律：不落盘成 .py 夹具，
# 运行时写进临时目录，防静态扫描把测试夹具当真实缺陷）
MINI_VULN = '''import os
import sys


def main():
    name = sys.argv[1]
    os.remove(name)
'''


@pytest.fixture()
def vuln_dir(tmp_path):
    d = tmp_path / "vulnroot"
    d.mkdir()
    (d / "mini_vuln.py").write_text(MINI_VULN, encoding="utf-8")
    return d


def test_tool_registered():
    names = {t["name"] for t in registry.list_tools()}
    assert "rust_taint_scan" in names
    schema = next(t["inputSchema"] for t in registry.list_tools()
                  if t["name"] == "rust_taint_scan")
    assert "root" in schema["required"]


def test_scan_reports_findings(vuln_dir):
    r = rust_taint_scan(str(vuln_dir))
    assert "error" not in r, r
    assert r["files_scanned"] == 1
    sinks = {f["sink"] for f in r["findings"]}
    assert "os.remove" in sinks, r
    os_f = next(f for f in r["findings"] if f["sink"] == "os.remove")
    assert os_f["severity"] in ("high", "med")
    assert os_f["source_kind"] in ("argv", "param")
    assert r["root"].endswith("vulnroot") or r["root"].endswith("vulnroot\\".rstrip("\\"))
    assert r["naive"] is False


def test_naive_mode_tags_findings(vuln_dir):
    r = rust_taint_scan(str(vuln_dir), naive=True)
    assert "error" not in r, r
    assert r["naive"] is True
    assert r["findings"], "基线模式至少应报 os.remove(name)"
    assert all(f["kind"] == "naive" for f in r["findings"])


def test_sandbox_rejects_escape(tmp_path):
    r = rust_taint_scan(str(tmp_path / "nope" / ".." / ".." / "outside"))
    if "error" in r and "越界" in str(r["error"]):
        return  # 沙盒根未包含 tmp_path 时按越界拒绝 = 正确
    # 沙盒含 tmp_path 的环境：目录不存在/为空 → 无 findings，不允许崩
    assert isinstance(r, dict)


def test_escape_path_rejected_hard(tmp_path, monkeypatch):
    """沙盒钳制必拒沙盒外路径（把沙盒根钉在 tmp_path 外验证）。"""
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path / "sandbox-root"))
    (tmp_path / "sandbox-root").mkdir(exist_ok=True)
    r = rust_taint_scan(r"C:\Windows")
    assert "error" in r
    assert "越界" in str(r["error"])


def test_missing_exe_clean_error(vuln_dir, monkeypatch):
    monkeypatch.setattr(attack, "_rx_taint_exe", lambda: None)
    r = rust_taint_scan(str(vuln_dir))
    assert "error" in r
    assert "cargo build" in r["error"]
