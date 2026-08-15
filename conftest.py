# -*- coding: utf-8 -*-
"""全局测试夹具：每个测试后停后台扫描循环（2026-08-15 flaky 根治）。

后台线程（_spawn_self_scan 的 self/project/full 循环）跨测试存活时会
写后续测试的 env 日志（root=旧 tmp 目录）→ shadow 测试偶发扫到残留
候选。autouse fixture 对所有测试文件生效——测试后置停止标志。
"""

import pytest


@pytest.fixture(autouse=True)
def _stop_scan_loops_global():
    yield
    try:
        import server
        server._stop_scan_loops()
        server._SCAN_LOOPS_STOP = False
    except Exception:
        pass
