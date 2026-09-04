# -*- coding: utf-8 -*-
"""S79 fs 读面 Rust 化契约测试：薄壳（tools/fs.py）→ rx-fs.exe 的包络与行为。

S79 起旧 Python 实现的职责移入 Rust（tests at rust/tests/fs_test.rs 打同一语义）；
本文件守住 Python 侧注册面契约：
- 沙盒拒绝 = ValueError 包络（registry ok:false），工具级错误 = result.error 字段；
- universal newlines 归一 / 1MB 上限 / 深度语义（0=仅根层，S79 归正）；
- exe 缺失走清晰报错，不静默降级。
"""
import os

import pytest

import registry
import tools  # noqa: F401


@pytest.fixture()
def open_sandbox(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")


# ---------- fs_read ----------

def test_read_normalizes_universal_newlines(tmp_path, open_sandbox):
    p = tmp_path / "w.txt"
    p.write_bytes(b"a\r\nb\rc\n")
    r = registry.call("fs_read", {"path": str(p)})
    assert r["ok"], r
    assert r["result"]["content"] == "a\nb\nc\n"
    assert r["result"]["size"] == 7  # 字节数，替换前大小


def test_read_oversize_rejected_with_size(tmp_path, open_sandbox):
    p = tmp_path / "big.bin"
    p.write_bytes(b"A" * 1_000_001)
    r = registry.call("fs_read", {"path": str(p)})
    # registry 对 {"error":...} 结果统一转 ok:false（error 顶层 + result 保留）
    assert not r["ok"] and "文件过大" in r["error"]
    assert r["result"]["size"] == 1_000_001


def test_read_not_a_file(tmp_path, open_sandbox):
    r = registry.call("fs_read", {"path": str(tmp_path)})
    assert not r["ok"] and "不是文件或不存在" in r["error"]


# ---------- fs_stat ----------

def test_stat_real_and_ghost(tmp_path, open_sandbox):
    p = tmp_path / "x.txt"
    p.write_text("hi", encoding="utf-8")
    r = registry.call("fs_stat", {"path": str(p)})
    assert r["ok"] and r["result"]["exists"] is True
    assert r["result"]["is_file"] is True and r["result"]["size"] == 2
    assert isinstance(r["result"]["mtime"], int)
    ghost = tmp_path / "no" / "such.txt"  # 沙盒内不存在路径：exists:false（宽限 realpath）
    r2 = registry.call("fs_stat", {"path": str(ghost)})
    assert r2["ok"] and r2["result"]["exists"] is False


# ---------- fs_list ----------

def _make_tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.txt").write_text("1", encoding="utf-8")
    (tmp_path / "b" / "sub").mkdir(parents=True)
    (tmp_path / "b" / "sub" / "3.txt").write_text("3", encoding="utf-8")
    (tmp_path / "2.txt").write_text("2", encoding="utf-8")


def test_list_depth_semantics_and_order(tmp_path, open_sandbox):
    _make_tree(tmp_path)
    r0 = registry.call("fs_list", {"path": str(tmp_path), "depth": 0})
    assert r0["ok"] and r0["result"]["total"] == 3, "S79 归正：0=仅根层"

    r1 = registry.call("fs_list", {"path": str(tmp_path)})
    names = [e["name"] for e in r1["result"]["entries"]]
    assert r1["result"]["total"] == 5
    assert names == ["2.txt", "a", "a\\1.txt", "b", "b\\sub"], names
    a_dir = next(e for e in r1["result"]["entries"] if e["name"] == "a")
    assert a_dir["type"] == "dir" and "size" not in a_dir
    f = next(e for e in r1["result"]["entries"] if e["name"] == "a\\1.txt")
    assert f["type"] == "file" and f["size"] == 1


def test_list_not_a_dir(tmp_path, open_sandbox):
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    r = registry.call("fs_list", {"path": str(p)})
    assert not r["ok"] and "不是目录" in r["error"]


# ---------- 沙盒包络（旧实现是 ValueError → ok:false，薄壳必须同包络） ----------

def test_sandbox_deny_valueerror_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "Z:\\no-such-root-xyz")
    p = tmp_path / "w.txt"
    p.write_text("x", encoding="utf-8")
    r = registry.call("fs_read", {"path": str(p)})
    assert not r["ok"] and "路径越界" in r["error"]


def test_fail_closed_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_RX_SANDBOX", raising=False)
    r = registry.call("fs_read", {"path": str(tmp_path)})
    assert not r["ok"]


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    # 隔掉 env 覆盖与 TEMP 惯例路径两个候选源，验证"缺失=清晰报错"而非静默降级
    bogus = tmp_path / "not-an-exe.exe"
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", str(bogus))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")
    r = registry.call("fs_read", {"path": str(tmp_path)})
    assert not r["ok"] and "rx-fs.exe 不存在" in r["error"]


# ---------- 注册面契约（S72b） ----------

def test_schemas_unchanged():
    assert registry._TOOLS["fs_read"]["schema"]["required"] == ["path"]
    assert "__authorized" not in registry._TOOLS["fs_stat"]["schema"]["properties"]
    assert registry._TOOLS["fs_write"]["requires_auth"] is True
    assert registry._TOOLS["fs_list"]["group"] == "fs"
