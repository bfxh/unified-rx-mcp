#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""dashboard（运行仪表盘）测试：API 函数 + HTTP 层（零依赖 urllib）。"""
import json
import os
import sys
import threading
import http.server
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard  # noqa: E402


def test_overview_shape():
    ov = dashboard._overview()
    assert ov["ok"] is True
    assert ov["tools"]["total"] >= 100
    assert ov["tools"]["core"] >= 60
    assert ov["stats_total"] > 0
    assert "files" in ov and "heartbeats" in ov
    # 数据文件应存在且新鲜（daemon/常驻在跑）
    assert ov["files"]["stats.json"]["age_s"] >= 0


def test_tools_list():
    t = dashboard._tools()
    assert t["core_count"] + t["ext_count"] == t["total"]
    assert "mesh" in t["core"] and "net_chaos" in t["core"]


def test_telemetry_shape():
    tel = dashboard._telemetry()
    assert tel["samples"] >= 0
    assert 0 <= tel["err_rate"] <= 1
    assert isinstance(tel["slowest"], list)


def test_scanlog_and_live():
    sl = dashboard._scanlog(5)
    assert isinstance(sl, list) and len(sl) <= 5
    lv = dashboard._live(5)
    assert isinstance(lv, list) and len(lv) <= 5
    for r in lv:
        assert "tool" in r and "ts" in r


@pytest.fixture(scope="module")
def http_base():
    """临时端口起真实 HTTP server（线程）。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_http_index(http_base):
    with urllib.request.urlopen(http_base + "/", timeout=5) as r:
        body = r.read().decode("utf-8")
    assert r.status == 200
    assert "unified-rx 运行仪表盘" in body
    assert "tick()" in body


def test_http_api(http_base):
    for path in ("/api/overview", "/api/tools", "/api/telemetry",
                 "/api/scanlog", "/api/live"):
        with urllib.request.urlopen(http_base + path, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        assert d["ok"] is True, f"{path}: {d}"
    # 未知路径 → 结构化错误
    with urllib.request.urlopen(http_base + "/api/nope", timeout=5) as r:
        d = json.loads(r.read().decode("utf-8"))
    assert d["ok"] is False
