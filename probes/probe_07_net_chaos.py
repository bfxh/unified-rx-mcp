"""probe_07：契约 §7 net_chaos（弱网模拟）。

验证（对齐 spec/07 的 MUST）：
  p07a 参数边界：loss/reorder 越界钳制、delay 负值钳制
  p07b 生命周期：start 返回 {ok,listen,target,cfg,pid}；重复 start 幂等
  p07c 数据一致性：无混沌往返字节级一致
  p07d 延迟注入：往返耗时 ≥ 配置延迟（双向注入 ≈ 2×delay）
  p07e 100% 丢包：不回显且不挂死
  p07f 失败语义：非法 action 报参数非法；stop 幂等
"""
import json
import os
import socket
import threading
import time

from _common import probe, REPO_ROOT
import server as S

_TMP = os.path.join(REPO_ROOT, "_probe_tmp")
os.makedirs(_TMP, exist_ok=True)


def _echo_server(port: int) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    while True:
        c, _ = srv.accept()

        def handler(conn):
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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _roundtrip(listen, payload, timeout=10.0):
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


@probe("p07a_param_clamp")
def p07a():
    """§7.2 参数边界：越界钳制到合法区间。"""
    # 工具层无直接暴露钳制，走 net_core 内部（契约实现层）
    import sys
    sys.path.insert(0, REPO_ROOT)
    import net_core
    cfg = net_core._cfg_dict(delay=-5, loss=150, reorder=-1, bandwidth=-3)
    expect = {"delay_ms": 0.0, "loss_pct": 100.0,
              "reorder_pct": 0.0, "bandwidth_kbps": 0}
    if cfg == expect:
        return True, f"钳制正确: {cfg}"
    return False, f"钳制失败: {cfg} != {expect}"


@probe("p07b_lifecycle")
def p07b():
    """§7.3 生命周期：start 结构/重复幂等/status/stop。"""
    port = _free_port()
    t = threading.Thread(target=_echo_server, args=(port,), daemon=True)
    t.start()
    time.sleep(0.2)
    out = S._call("net_chaos", {"action": "start", "target": f"127.0.0.1:{port}"})
    d = json.loads(out[0].text)
    if not d.get("ok") or "listen" not in d or "pid" not in d:
        return False, f"start 结构不符: {d}"
    listen = d["listen"]
    # 重复 start 幂等
    out2 = S._call("net_chaos", {"action": "start",
                                 "target": f"127.0.0.1:{port}", "listen": listen})
    d2 = json.loads(out2[0].text)
    if not d2.get("already_running"):
        return False, f"重复 start 未幂等: {d2}"
    # status 1 个
    st = json.loads(S._call("net_chaos", {"action": "status"})[0].text)
    if st.get("count") != 1:
        return False, f"status 期望 1 个: {st}"
    # stop 幂等
    sp1 = json.loads(S._call("net_chaos", {"action": "stop"})[0].text)
    sp2 = json.loads(S._call("net_chaos", {"action": "stop"})[0].text)
    if not sp1.get("ok") or len(sp1.get("stopped", [])) != 1:
        return False, f"stop 异常: {sp1}"
    if not sp2.get("ok"):
        return False, f"stop 非幂等: {sp2}"
    return True, "生命周期全过（start/幂等/status/stop/stop）"


@probe("p07c_data_integrity")
def p07c():
    """§7.5.1 无混沌：往返数据字节级一致。"""
    port = _free_port()
    threading.Thread(target=_echo_server, args=(port,), daemon=True).start()
    time.sleep(0.2)
    out = S._call("net_chaos", {"action": "start",
                                "target": f"127.0.0.1:{port}"})
    listen = json.loads(out[0].text)["listen"]
    try:
        payload = os.urandom(512)
        data, _ = _roundtrip(listen, payload)
        if data == payload:
            return True, "512B 随机数据一致"
        return False, f"数据不一致: {len(data)}/{len(payload)}B"
    finally:
        S._call("net_chaos", {"action": "stop"})


@probe("p07d_delay_injection")
def p07d():
    """§7.5.2 延迟注入：往返耗时 ≥ 配置延迟。"""
    port = _free_port()
    threading.Thread(target=_echo_server, args=(port,), daemon=True).start()
    time.sleep(0.2)
    out = S._call("net_chaos", {"action": "start",
                                "target": f"127.0.0.1:{port}", "delay": 150})
    listen = json.loads(out[0].text)["listen"]
    try:
        data, el = _roundtrip(listen, b"delay-check")
        if data == b"delay-check" and el >= 150:
            return True, f"延迟注入生效: {el:.0f}ms >= 150ms"
        return False, f"延迟失效: data={data!r} el={el:.0f}ms"
    finally:
        S._call("net_chaos", {"action": "stop"})


@probe("p07e_loss_drop")
def p07e():
    """§7.5.3 100% 丢包：不回显且不挂死。"""
    port = _free_port()
    threading.Thread(target=_echo_server, args=(port,), daemon=True).start()
    time.sleep(0.2)
    out = S._call("net_chaos", {"action": "start",
                                "target": f"127.0.0.1:{port}", "loss": 100})
    listen = json.loads(out[0].text)["listen"]
    try:
        c = socket.socket()
        c.settimeout(1.0)
        c.connect(("127.0.0.1", int(listen.rsplit(":", 1)[1])))
        c.sendall(b"will-drop")
        t0 = time.time()
        try:
            got = c.recv(64)
        except socket.timeout:
            got = b""
        c.close()
        if got != b"":
            return False, f"100% 丢包下收到回显: {got!r}"
        if time.time() - t0 > 2.0:
            return False, "丢包路径挂死"
        return True, "100% 丢包不回显、快速返回"
    finally:
        S._call("net_chaos", {"action": "stop"})


@probe("p07f_failure_semantics")
def p07f():
    """§7.4 失败语义：非法 action/参数类型报错；不可用明确 error。"""
    out = S._call("net_chaos", {"action": "bogus_action"})
    d = json.loads(out[0].text)
    if d.get("ok") or "error" not in d:
        return False, f"非法 action 未报错: {d}"
    out2 = S._call("net_chaos", {"action": "start", "delay": "not-a-number"})
    d2 = json.loads(out2[0].text)
    if d2.get("ok") or "error" not in d2:
        return False, f"非法参数未报错: {d2}"
    return True, "失败语义正确（非法 action/参数均结构化报错）"
