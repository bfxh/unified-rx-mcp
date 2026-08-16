#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""net_chaos 工具测试：弱网模拟（rx-net）桥接集成验证。

覆盖：参数校验（丢包/乱序/延迟边界）、自动端口、真实代理往返（数据一致 +
延迟注入生效）、启停生命周期、sanity 自检、双代理并存。rx-net 未编译时
跳过（CI 无 Rust 工具链时 net_core.enabled()=False）。
"""
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net_core  # noqa: E402

pytestmark = pytest.mark.skipif(
    not net_core.enabled(),
    reason="rx-net 未编译（RX_NET=0 或 exe 缺失）")


def _echo_server(port: int) -> None:
    """回显服务：原样写回（daemon 线程）。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    while True:
        c, _ = srv.accept()

        def handler(conn: socket.socket) -> None:
            try:
                while True:
                    d = conn.recv(4096)
                    if not d:
                        break
                    conn.sendall(d)
            except OSError:
                pass
            finally:
                conn.close()

        threading.Thread(target=handler, args=(c,), daemon=True).start()


@pytest.fixture(scope="module")
def echo_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t = threading.Thread(target=_echo_server, args=(port,), daemon=True)
    t.start()
    time.sleep(0.2)
    return port


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    net_core.stop()


def _roundtrip(listen: str, payload: bytes, timeout: float = 10.0) -> tuple:
    """经代理往返，返回 (数据, 毫秒)。"""
    c = socket.socket()
    c.settimeout(timeout)
    t0 = time.time()
    c.connect(("127.0.0.1", int(listen.rsplit(":", 1)[1])))
    c.sendall(payload)
    data = b""
    while len(data) < len(payload):
        chunk = c.recv(4096)
        if not chunk:
            break
        data += chunk
    c.close()
    return data, (time.time() - t0) * 1000


def test_param_clamp():
    cfg = net_core._cfg_dict(delay=-5, loss=150, reorder=-1, bandwidth=-3)
    assert cfg == {"delay_ms": 0.0, "loss_pct": 100.0,
                   "reorder_pct": 0.0, "bandwidth_kbps": 0}


def test_start_auto_port(echo_port):
    st = net_core.start(target=f"127.0.0.1:{echo_port}", delay=50)
    assert st and st["ok"] and st["listen"].startswith("127.0.0.1:")
    data, el = _roundtrip(st["listen"], b"auto-port-ok")
    assert data == b"auto-port-ok"
    assert el >= 50, f"延迟注入失效: {el}ms"


def test_delay_injection(echo_port):
    st = net_core.start(target=f"127.0.0.1:{echo_port}", delay=150)
    assert st and st["ok"]
    data, el = _roundtrip(st["listen"], b"delay-check")
    assert data == b"delay-check"
    assert el >= 150, f"延迟注入失效: {el}ms"


def test_loss_drops(echo_port):
    """丢包 100%：数据不回（代理丢弃后不转发）。"""
    st = net_core.start(target=f"127.0.0.1:{echo_port}", loss=100)
    assert st and st["ok"]
    c = socket.socket()
    c.settimeout(1.0)
    c.connect(("127.0.0.1", int(st["listen"].rsplit(":", 1)[1])))
    c.sendall(b"will-drop")
    t0 = time.time()
    try:
        d = c.recv(64)
        got = d
    except socket.timeout:
        got = b""
    c.close()
    assert got == b"", "100% 丢包下不应有回显"
    assert time.time() - t0 < 2.0, "丢包路径应快速返回（不应挂死）"


def test_duplicate_start_idempotent(echo_port):
    st1 = net_core.start(target=f"127.0.0.1:{echo_port}")
    st2 = net_core.start(target=f"127.0.0.1:{echo_port}",
                         listen=st1["listen"])
    assert st2["already_running"] is True
    assert st2["pid"] == st1["pid"]
    assert net_core.status()["count"] == 1


def test_multi_proxy(echo_port):
    st1 = net_core.start(target=f"127.0.0.1:{echo_port}", delay=30)
    st2 = net_core.start(target=f"127.0.0.1:{echo_port}", delay=30)
    assert st1["ok"] and st2["ok"] and st1["listen"] != st2["listen"]
    assert net_core.status()["count"] == 2
    for st in (st1, st2):
        data, _ = _roundtrip(st["listen"], b"multi")
        assert data == b"multi"


def test_stop_all(echo_port):
    net_core.start(target=f"127.0.0.1:{echo_port}")
    net_core.start(target=f"127.0.0.1:{echo_port}")
    st = net_core.stop()
    assert st["ok"] and len(st["stopped"]) == 2
    assert net_core.status()["count"] == 0


def test_sanity():
    r = net_core.sanity()
    assert r and r["ok"], r
    assert "ms" in r["result"] and "B" in r["result"]


def test_sanity_with_chaos():
    r = net_core.sanity(delay=100)
    assert r and r["ok"], r
    ms = int(r["result"].split("sanity ok: ", 1)[1].split("ms")[0])
    assert ms >= 100, f"sanity 延迟注入失效: {ms}ms"
