# -*- coding: utf-8 -*-
"""S78 协议层 fuzz 电池（spec/VULN-HUNTING.md P1-c）。

双靶打 stdio 协议层：靶 "python" = 现役 server.py；靶 "rust" = S78 rx-mcp.exe
（exe 不存在则 skip——`cargo build --release` 产出，路径可用 UNIFIED_RX_RS_EXE 覆盖）。
标准继承 input_fuzz 的"绝不崩"：任意畸形输入下进程必须要么持续应答要么干净退出
（EOF returncode==0），每条请求至多一条响应，响应必须是合法 jsonrpc 2.0 信封，
有 id 的请求按 id 精确配对。崩溃类（S78 实锤四处）：
  ① 顶层非对象消息（[] → server.py msg.get AttributeError）
  ② notifications/cancelled params 非对象（params or {} 后 .get 崩）
  ③ 深嵌套 RecursionError 从 json.loads 漏出
  ④ 无 id 的未知方法被回包（污染宿主 id 配对）
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_TMP = os.environ.get("TEMP", r"C:\\Users\\lbx13\\AppData\\Local\\Temp")
RS_EXE = os.environ.get("UNIFIED_RX_RS_EXE") or next(
    (os.path.join(_TMP, kind, "rx-mcp.exe")
     for kind in ("rx-rs-target\\release", "rx-rs-target\\debug")
     if os.path.isfile(os.path.join(_TMP, kind, "rx-mcp.exe"))),
    None,
)


def _py_cmd():
    return [sys.executable, os.path.join(ROOT, "server.py")]


TARGETS = [("python", _py_cmd())]
if RS_EXE:
    TARGETS.append(("rust", [RS_EXE]))


def _talk(cmd, lines, timeout=60):
    """喂原始字节行、读到 EOF。返回 (returncode, 响应列表, stderr 尾部)。

    断言前置：超时视为挂死直接 fail（协议层吃死 = 最恶性失败）。
    """
    env = dict(os.environ)
    env["UNIFIED_RX_SANDBOX"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    env["UNIFIED_RX_AUTOPILOT_VSCODE"] = "0"
    env.setdefault("UNIFIED_RX_AUTOPILOT_ROOT", _TMP)  # 兜底：autopilot 空转
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    try:
        out, err = p.communicate(b"".join(lines), timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        pytest.fail("协议靶 %ss 内未退出（挂死）" % timeout)
    resps = []
    for ln in out.splitlines():
        if ln.strip():
            resps.append(json.loads(ln.decode("utf-8", errors="replace")))
    return p.returncode, resps, err.decode("utf-8", errors="replace")[-800:]


def _ping(rid, params=None):
    m = {"jsonrpc": "2.0", "id": rid, "method": "ping"}
    if params is not None:
        m["params"] = params
    return (json.dumps(m, ensure_ascii=False) + "\n").encode("utf-8")


def _assert_envelopes(resps):
    """每条响应：合法信封、result/error 恰具其一。"""
    for r in resps:
        assert isinstance(r, dict), "响应非对象: %r" % (r,)
        assert r.get("jsonrpc") == "2.0", "jsonrpc 字段坏: %r" % (r,)
        assert "id" in r, "响应缺 id: %r" % (r,)
        assert ("result" in r) != ("error" in r), \
            "result/error 必须恰具其一: %r" % (r,)


def _assert_ping_ok(resps, rid):
    """id=rid 的 ping 恰好一条成功响应。"""
    hits = [r for r in resps if r.get("id") == rid and "result" in r]
    assert len(hits) == 1, "ping(id=%r) 应恰一条成功响应, 实得: %r" % (rid, resps)


@pytest.mark.parametrize("tname,cmd", TARGETS, ids=[t[0] for t in TARGETS])
class TestProtocolFuzz:
    """每个用例独立进程：喂敌意行 → 末尾 ping 探活。"""

    # ① 顶层非对象（json.loads 得 list/int/str/None/bool）
    def test_non_object_messages(self, tname, cmd):
        rc, resps, err = _talk(cmd, [
            b'[]\n', b'[1,2,3]\n', b'123\n', b'"str"\n', b'null\n', b'true\n',
            b'-1.5\n',
            _ping("alive-1"),
        ])
        assert rc == 0, "非对象消息打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "alive-1")

    # ② notifications/cancelled 的 params 非对象（list/str/int）
    def test_cancelled_params_wrong_type(self, tname, cmd):
        rc, resps, err = _talk(cmd, [
            b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":[]}\n',
            b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":"x"}\n',
            b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":7}\n',
            _ping("alive-2"),
        ])
        assert rc == 0, "cancelled params 打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "alive-2")

    # ③ 深嵌套：json.loads RecursionError 必须被吞（合法浅嵌套不受影响）
    # 恶意深度取 20 万：3.11 的 C 递归上限 ~1000、3.13+（Windows）抬到 ~5000+
    # ——固定 3000 在新解释器下会被合法解析（钩子用 python=3.14 首跑实锤），
    # 20 万层在任何 CPython 都必然 RecursionError，契约"解析失败→静默"才稳定
    def test_deep_nesting(self, tname, cmd):
        deep_ok = "[" * 100 + "]" * 100          # 合法：应正常应答
        deep_evil = "[" * 200000 + "]" * 200000  # 超一切 C 递归限：必须吞掉不崩
        rc, resps, err = _talk(cmd, [
            _ping("deep-ok", params={"n": json.loads(deep_ok)}),
            ('{"jsonrpc":"2.0","id":"deep-evil","method":"ping","params":%s}\n'
             % deep_evil).encode(),
            _ping("alive-3"),
        ])
        assert rc == 0, "深嵌套打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "deep-ok")
        _assert_ping_ok(resps, "alive-3")
        assert not any(r.get("id") == "deep-evil" for r in resps), \
            "解析失败的消息不得产出响应"

    # ④ 通知永不回包：未知通知风暴后必须能干净应答 ping（排水检查）
    def test_notifications_never_replied(self, tname, cmd):
        storm = [b'{"jsonrpc":"2.0","method":"x/y%d"}\n' % i for i in range(50)]
        rc, resps, err = _talk(cmd, storm + [_ping("alive-4")])
        assert rc == 0, "通知风暴打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "alive-4")
        assert len(resps) == 1, "通知必须静默（无 id 不回包），实得 %d 条: %r" % (
            len(resps), resps)

    # ⑤ id 动物园：各型 id 原样回显（JSON-RPC 宿主靠 id 配对）
    @pytest.mark.parametrize("rid", [0, -1, 1.5, "s", None, 2 ** 70, [1], {"a": 1}],
                             ids=["0", "neg", "float", "str", "null",
                                  "big", "arr", "obj"])
    def test_id_roundtrip(self, tname, cmd, rid):
        rc, resps, err = _talk(cmd, [_ping(rid)])
        assert rc == 0, err
        _assert_envelopes(resps)
        hits = [r for r in resps if r.get("id") == rid and "result" in r]
        assert len(hits) == 1, "id=%r 未原样回显: %r" % (rid, resps)

    # ⑥ 畸形 JSON / 二进制 / 截断行 / BOM / CRLF / 空行
    def test_malformed_bytes(self, tname, cmd):
        bom_ping = (b'\xef\xbb\xbf' + _ping("alive-6").rstrip(b"\n") + b"\r\n")
        rc, resps, err = _talk(cmd, [
            b'{"jsonrpc":"2.0","id":1,"method":"ping"\n',   # 截断
            b'not json at all\n',
            b'\xff\xfe\x00\x01binary\n',
            b'\r\n', b'   \n', b'\n',                        # 空行
            bom_ping,                                        # BOM + CRLF
            _ping("alive-6b"),
        ])
        assert rc == 0, "畸形字节打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "alive-6b")

    # ⑦ tools/call 敌意面：未知工具 / arguments 非对象 / 沙盒逃逸路径
    def test_tools_call_hostile(self, tname, cmd, tmp_path):
        # rust_taint_scan 的 root 给真实存在的小目录（沙盒 "*" 下 "../../"
        # 会合法扫描整个开发盘——是慢不是漏洞，电池要快且确定）
        tiny = tmp_path / "tiny-root"
        tiny.mkdir()
        (tiny / "one.py").write_text("x = 1\n", encoding="utf-8")
        root_arg = str(tiny).replace("\\", "/")
        rc, resps, err = _talk(cmd, [
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"nonexistent_zz","arguments":{}}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
            b'"params":{"name":"x","arguments":[]}}\n',
            ('{"jsonrpc":"2.0","id":3,"method":"tools/call",'
             '"params":{"name":"rust_taint_scan","arguments":{"root":"%s"}}}\n'
             % root_arg).encode(),
            b'{"jsonrpc":"2.0","id":4,"method":"tools/call",'
            b'"params":{"name":"fs_read","arguments":{"path":"../../etc/passwd"}}}\n',
            _ping("alive-7"),
        ])
        assert rc == 0, "敌意 tools/call 打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "alive-7")
        # 四条调用每条至多一条响应且 id 正确（不崩、不串号）
        for rid in (1, 2, 3, 4):
            got = [r for r in resps if r.get("id") == rid]
            assert len(got) <= 1, "id=%r 重复响应: %r" % (rid, got)

    # ⑧ 超长行 / 超长字段（64MB 上限内）：不应撑爆也不应卡死
    def test_big_line(self, tname, cmd):
        big = "A" * 1_000_000
        rc, resps, err = _talk(cmd, [
            ('{"jsonrpc":"2.0","id":"big","method":"ping",'
             '"params":{"x":"%s"}}\n' % big).encode(),
            _ping("alive-8"),
        ])
        assert rc == 0, "大行打崩靶: %s" % err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "big")
        _assert_ping_ok(resps, "alive-8")

    # ⑨ 协议杂项：jsonrpc 缺省放行 / 错误版本拒 / 未知方法结构化报错
    def test_jsonrpc_semantics(self, tname, cmd):
        rc, resps, err = _talk(cmd, [
            b'{"id":"nok","method":"ping"}\n',                # 缺 jsonrpc → 放行
            b'{"jsonrpc":"1.0","id":"bad","method":"ping"}\n',  # 错版本 → -32600
            b'{"jsonrpc":"2.0","id":"unk","method":"no/such"}\n',  # 未知方法
            _ping("alive-9"),
        ])
        assert rc == 0, err
        _assert_envelopes(resps)
        _assert_ping_ok(resps, "nok")
        _assert_ping_ok(resps, "alive-9")
        bad = [r for r in resps if r.get("id") == "bad"]
        unk = [r for r in resps if r.get("id") == "unk"]
        assert len(bad) == 1 and "error" in bad[0], "错版本必须报错: %r" % bad
        assert len(unk) == 1, "未知方法必须有结构化响应: %r" % unk
