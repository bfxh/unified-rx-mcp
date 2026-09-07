# -*- coding: utf-8 -*-
"""S90 fs_write Rust 化契约测试：fs 域 4/4 收官（写面走 rx-fs.exe）。

守住三层契约：
- 行为等价：字节保真（stdin 二进制通道，无换行翻译）、size=Unicode 字符数、
  1MB 上限（先于沙盒检查）、越界 ValueError 包络、失败不留 urxtmp 残渣；
- 退役断言：fs.py 不再直接写盘（os.replace / urxtmp 全退场），_resolve 保留
  （oracle 锚 + scan/search/game/ops 的导入面）；
- 顺带修回归门（S90）：scan/search 的 stdin 通道同步二进制化——强制 stdin
  路径与 argv 路径结果一致（text 模式 stdin 的 \\n→os.linesep 翻译不许回归）。
"""
from pathlib import Path

import pytest

import registry
import tools  # noqa: F401


@pytest.fixture()
def open_sandbox(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")


def _write(path, content):
    return registry.call("fs_write", {"path": str(path), "content": content,
                                      "__authorized": True})


# ---------- 行为等价 ----------

def test_write_bytes_verbatim_and_char_size(tmp_path, open_sandbox):
    p = tmp_path / "w.txt"
    content = "你好\nworld\r\ncarriage\rlf"
    r = _write(p, content)
    assert r["ok"], r
    # 字节保真：链路上无任何换行翻译（S90 核心回归门——text 模式 stdin 会
    # 把 \n 翻成 \r\n；二进制字节通道必须原样落盘）
    assert p.read_bytes() == content.encode("utf-8")
    # size = Unicode 字符数（等价旧实现 len(str)），非字节数
    assert r["result"]["size"] == len(content)
    assert r["result"]["ok"] is True


def test_write_roundtrip_overwrite_and_empty(tmp_path, open_sandbox):
    p = tmp_path / "r.txt"
    assert _write(p, "v1")["ok"]
    r = _write(p, "你好")  # 覆盖写
    assert r["ok"] and r["result"]["size"] == 2
    assert p.read_bytes() == "你好".encode("utf-8")
    assert _write(p, "")["ok"]  # 空内容 → 空文件
    assert p.read_bytes() == b""


def test_write_size_cap_exact_and_over(tmp_path, open_sandbox):
    ok = _write(tmp_path / "cap.txt", "A" * 1_000_000)
    assert ok["ok"] and ok["result"]["size"] == 1_000_000
    over = _write(tmp_path / "over.txt", "A" * 1_000_001)
    # registry 对 {"error":...} 结果统一 ok:false；消息与旧实现逐字一致
    assert not over["ok"]
    assert "内容过大（1000001 > 1000000 字节）" in over["error"]


def test_write_cap_beats_sandbox_check(tmp_path, monkeypatch):
    # 顺序对齐旧实现：先大小上限后沙盒 resolve（超大+越界同中 → "内容过大"先报）
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    r = _write(r"C:\Windows\s90-probe.txt", "A" * 1_000_001)
    assert not r["ok"] and "内容过大" in r["error"]
    assert "路径越界" not in r["error"]


def test_write_outside_sandbox_valueerror_envelope(tmp_path, monkeypatch):
    # resolve 层拒绝 → exe 退出码 2 → ValueError → registry ok:false（同旧包络）
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "Z:\\no-such-root-xyz")
    r = _write(tmp_path / "w.txt", "x")
    assert not r["ok"] and "路径越界" in r["error"]


def test_write_requires_auth_gate(tmp_path, open_sandbox):
    p = tmp_path / "noauth.txt"
    r = registry.call("fs_write", {"path": str(p), "content": "x"})
    assert not r["ok"], "无授权不应写入"
    assert not p.exists()


def test_write_fail_envelopes_and_no_residue(tmp_path, open_sandbox):
    # 目录为目标：replace 必败 → "写入失败" 包络（OS 错误文本两侧发散，已掩码口径）
    d = tmp_path / "adir"
    d.mkdir()
    r = _write(d, "x")
    assert not r["ok"] and "写入失败" in r["error"]
    # 父路径是文件：create_dir_all 必败 → "创建目录失败" 包络
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    r2 = _write(f / "sub.txt", "x")
    assert not r2["ok"] and "创建目录失败" in r2["error"]
    # 两种失败都不得留 urxtmp 半截文件（S62 原子写纪律在 exe 侧延续）
    assert list(tmp_path.rglob("*.urxtmp*")) == []


def test_write_exe_missing_clear_error(tmp_path, monkeypatch):
    # 隔掉 env 覆盖与 TEMP 惯例路径两个候选源：缺失=清晰报错，不静默降级
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", str(tmp_path / "not-an-exe.exe"))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")
    r = _write(tmp_path / "x.txt", "hi")
    assert not r["ok"] and "rx-fs.exe 不存在" in r["error"]


# ---------- 注册面契约 + 退役断言 ----------

def test_s90_registration_contracts():
    t = registry._TOOLS["fs_write"]
    assert t["schema"]["required"] == ["path", "content"]
    assert "__authorized" in t["schema"]["properties"]
    assert t["requires_auth"] is True


def test_s90_fs_py_is_thin_shell():
    src = (Path(__file__).resolve().parent.parent / "tools" / "fs.py").read_text(
        encoding="utf-8")
    # 写盘原语退役：fs.py 不再直接落盘（实现在 rust/src/fs.rs::op_write）
    assert "os.replace" not in src
    assert "urxtmp" not in src
    # _resolve 保留：oracle 锚 + scan/search/game/ops 仍导入
    assert "def _resolve(" in src
    assert '_rx_fs_call("write"' in src


# ---------- 顺带修回归门：stdin 二进制通道与 argv 通道结果一致 ----------

def test_search_stdin_channel_parity(tmp_path, monkeypatch, open_sandbox):
    import tools.search as searchmod
    (tmp_path / "mod_alpha.py").write_text(
        "def alpha_probe():\n    return 'needle_token'\n", encoding="utf-8")
    q = "needle_token 探针 query"
    r_argv = registry.call("code_search", {"query": q, "root": str(tmp_path), "k": 5})
    assert r_argv["ok"], r_argv
    monkeypatch.setattr(searchmod, "_QUERY_ARGV_CAP", 5)  # 强制 query 走 stdin
    r_stdin = registry.call("code_search", {"query": q, "root": str(tmp_path), "k": 5})
    assert r_stdin == r_argv


def test_semantic_stdin_channel_parity(tmp_path, monkeypatch, open_sandbox):
    import tools.search as searchmod
    (tmp_path / "m.py").write_text(
        "class Widget:\n    def draw(self):\n        pass\n", encoding="utf-8")
    q = "draw widget 渲染"
    r_argv = registry.call("code_semantic",
                           {"query": q, "root": str(tmp_path), "mode": "search", "k": 5})
    assert r_argv["ok"], r_argv
    monkeypatch.setattr(searchmod, "_QUERY_ARGV_CAP", 5)
    r_stdin = registry.call("code_semantic",
                            {"query": q, "root": str(tmp_path), "mode": "search", "k": 5})
    assert r_stdin == r_argv


def test_bug_locate_stdin_channel_parity(tmp_path, monkeypatch, open_sandbox):
    import tools.scan as scanmod
    err = ("Traceback (most recent call last):\n"
           "  File \"boom.py\", line 42, in <module>\n"
           "ValueError: boom 探针")
    r_argv = registry.call("bug_locate", {"error_text": err, "root": str(tmp_path)})
    assert r_argv["ok"], r_argv
    monkeypatch.setattr(scanmod, "_QUERY_ARGV_CAP", 5)  # 强制 error_text 走 stdin
    r_stdin = registry.call("bug_locate", {"error_text": err, "root": str(tmp_path)})
    assert r_stdin == r_argv
