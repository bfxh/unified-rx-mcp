# -*- coding: utf-8 -*-
"""全局测试夹具：每个测试后停后台扫描循环（2026-08-15 flaky 根治）。

后台线程（_spawn_self_scan 的 self/project/full 循环）跨测试存活时会
写后续测试的 env 日志（root=旧 tmp 目录）→ shadow 测试偶发扫到残留
候选。autouse fixture 对所有测试文件生效——测试后置停止标志。
"""

import os
import tempfile

import pytest

# 测试沙箱适配：server.py 在 import 时读取 UNIFIED_RX_SANDBOX（默认 cwd），
# 而测试的临时目录在系统 Temp 下——不注入会导致 repo_wiki/bug_scan 等
# 工具对测试 tmp 路径报"路径越界"。必须在任何 server import 前设置。
_test_tmp = tempfile.gettempdir()
_existing_roots = os.environ.get("UNIFIED_RX_SANDBOX", os.getcwd()).split(";")
if _test_tmp not in _existing_roots:
    os.environ["UNIFIED_RX_SANDBOX"] = ";".join(_existing_roots + [_test_tmp])


@pytest.fixture(autouse=True)
def _stop_scan_loops_global():
    yield
    try:
        import server
        server._stop_scan_loops()
        # 2026-08-15：不复位 STOP（security-review LOW：复位后后台循环
        # sleep 结束看到 False 继续跑）——spawn 测试显式复位（start 前）
    except Exception:
        pass
