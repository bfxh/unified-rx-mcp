# -*- coding: utf-8 -*-
"""S146 契约：协议双支持（2025-06-18 + 2025-03-26）+ 留痕可归因 + 账本隔离。

背景（EXTERNAL-ALIGNMENT B1 决策落定）：宿主实际请求版本仍待其重启入册，但
2025-06-18 变更单已逐条核对（合规矩阵见该文档）——确定**双支持**并按规范回显。
四锁：
1. 协商——白名单两版皆回显；未知/缺省 → 回我方最高支持（2025-06-18）；
2. 2025-06-18 面——tools/list 顶层 `title`（与 annotations.title 同值双发）；
3. 留痕可归因——params_keys/pid/server 在册（首轮 4 条 null 归因困难的补丁）；
4. 账本隔离回归——套件内的握手**不得**写进真实 ~/.unified-rx/clients.jsonl。
"""
import json
import os

import server

REAL_LOG = os.path.join(os.path.expanduser("~"), ".unified-rx", "clients.jsonl")


def _init(proto=None, client=None):
    params = {}
    if proto is not None:
        params["protocolVersion"] = proto
    if client is not None:
        params["clientInfo"] = client
    return server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": params})


def test_negotiation_double_support():
    assert server.PROTOCOL_VERSION == "2025-06-18"
    assert server._negotiate_version("2025-06-18") == "2025-06-18"
    assert server._negotiate_version("2025-03-26") == "2025-03-26"
    assert server._negotiate_version("2026-07-28") == server.PROTOCOL_VERSION
    assert server._negotiate_version(None) == server.PROTOCOL_VERSION


def test_tools_list_has_top_level_title():
    r = server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    for t in r["result"]["tools"]:
        assert t["title"] == t["annotations"]["title"], t["name"]


def test_handshake_record_attributable(tmp_path):
    orig = os.environ.get("UNIFIED_RX_CLIENTS_LOG")
    p = tmp_path / "c.jsonl"
    os.environ["UNIFIED_RX_CLIENTS_LOG"] = str(p)
    try:
        _init("2025-06-18", {"name": "zcode", "version": "1.0"})
        _init()          # 空 params（首轮 null 场景）——必须仍可归因
        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        assert lines[0]["requested"] == "2025-06-18"
        assert lines[0]["negotiated"] == "2025-06-18"
        assert lines[0]["client"] == "zcode" and lines[0]["client_version"] == "1.0"
        assert lines[1]["requested"] is None
        assert lines[1]["params_keys"] == []
        assert isinstance(lines[1]["pid"], int) and lines[1]["server"]
    finally:
        if orig is None:
            os.environ.pop("UNIFIED_RX_CLIENTS_LOG", None)
        else:
            os.environ["UNIFIED_RX_CLIENTS_LOG"] = orig


def test_test_context_never_writes_real_ledger(monkeypatch):
    """S146 加固锁：测试上下文（PYTEST_CURRENT_TEST）×无显式 env → 不落账本。

    背景实锤：全量套件出现"隔离变量在子进程丢失"的幽灵写入（ppid=pytest、params
    空/仅 protocolVersion）——源头堵法 = _clients_path() 在测试上下文返回 None。
    断言用 pid 精确判定（对并发宿主写入免疫）。
    """
    monkeypatch.delenv("UNIFIED_RX_CLIENTS_LOG", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::y (call)")
    assert server._clients_path() is None, "测试上下文必须不落账本"

    def _pids():
        if not os.path.isfile(REAL_LOG):
            return set()
        with open(REAL_LOG, encoding="utf-8") as f:
            return {json.loads(x).get("pid") for x in f if x.strip()}

    before = _pids()
    _init("2025-06-18")
    assert os.getpid() not in (_pids() - before), "本进程写进了真实账本"
