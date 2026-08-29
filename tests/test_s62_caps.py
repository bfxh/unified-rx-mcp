# -*- coding: utf-8 -*-
"""S62 加固轮回归钉：入站尺寸上限（协议行 / registry 参数 / LSP 帧）+ 原子写。"""
import io
import os
import queue
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import lsp as lsp_mod  # noqa: E402
import server  # noqa: E402

AUTH = {"__authorized": True}


def _fake_stdin(monkeypatch, data: bytes):
    buf = io.BytesIO(data)

    class _B:
        def readline(self, n=-1):
            return buf.readline(n)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=_B()))


# ---------- server 协议行上限 ----------

def test_read_line_normal_and_eof(monkeypatch):
    _fake_stdin(monkeypatch, b'{"a": 1}\n{"b": 2}\n')
    assert server._read_line() == '{"a": 1}'
    assert server._read_line() == '{"b": 2}'
    assert server._read_line() is None


def test_read_line_oversized_drained(monkeypatch):
    """超限行：丢弃整行，且残留不污染下一条消息。"""
    big = b"x" * (server._MAX_LINE_BYTES + 10)
    _fake_stdin(monkeypatch, big + b"\n" + b'{"ok": 1}\n')
    assert server._read_line() == ""           # 超限行被丢
    assert server._read_line() == '{"ok": 1}'  # 下一条完好
    assert server._read_line() is None


def test_read_line_at_cap_is_kept(monkeypatch):
    data = b"a" * server._MAX_LINE_BYTES + b"\n"
    _fake_stdin(monkeypatch, data)
    assert len(server._read_line()) == server._MAX_LINE_BYTES


# ---------- registry 入口尺寸门 ----------

def test_registry_rejects_huge_string_arg(tmp_path):
    huge = "A" * (registry._MAX_STR_ARG + 1)
    r = registry.call("fs_read", {"path": huge})
    assert not r["ok"] and "过大" in r["error"]


def test_registry_rejects_huge_list_arg(tmp_path):
    (tmp_path / "t.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ide_edit_multi", {**AUTH, "file_path": str(tmp_path / "t.py"),
                                         "edits": [{"old_lines": ["x = 1"],
                                                    "new_lines": ["x = 2"]}]})
    assert r["ok"], r.get("error")
    big_list = [{"old_lines": ["x = 1"], "new_lines": ["x = 2"]}] * \
        (registry._MAX_LIST_ARG + 1)
    r2 = registry.call("ide_edit_multi", {**AUTH, "file_path": str(tmp_path / "t.py"),
                                          "edits": big_list})
    assert not r2["ok"] and "列表过长" in r2["error"]


# ---------- LSP 入站帧上限 ----------

def _session_with_bytes(payload: bytes):
    s = lsp_mod._Session("python", ["x"], ".")
    s.proc = SimpleNamespace(stdout=io.BytesIO(payload))
    s._alive = True
    s._q = queue.Queue()
    return s


def test_lsp_reader_rejects_oversized_frame():
    hdr = b"Content-Length: 99999999999\r\n\r\n"
    s = _session_with_bytes(hdr + b"junk")
    s._reader()                                # 同步跑完（异常 → 哨兵）
    assert s._q.get_nowait() is None           # 连接哨兵 = reader 退出


def test_lsp_reader_accepts_normal_frame():
    body = b'{"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}'
    payload = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
    s = _session_with_bytes(payload)
    s._reader()
    msg = s._q.get_nowait()
    assert msg["id"] == 1


# ---------- 原子写 ----------

def test_fs_write_atomic_no_residue(tmp_path):
    f = tmp_path / "a.txt"
    r = registry.call("fs_write", {**AUTH, "path": str(f), "content": "hello"})
    assert r["ok"] and f.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.glob("*.urxtmp*")), "tmp 残留 = 原子写没做干净"


def test_edit_atomic_no_residue(tmp_path):
    f = tmp_path / "b.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ide_edit_multi", {**AUTH, "file_path": str(f),
                                         "edits": [{"old_lines": ["x = 1"],
                                                    "new_lines": ["x = 2"]}]})
    assert r["ok"] and f.read_text(encoding="utf-8") == "x = 2\n"
    assert not list(tmp_path.glob("*.urxtmp*"))
