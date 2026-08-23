# -*- coding: utf-8 -*-
"""conftest.py —— pytest 全局配置：tmp_path 基目录设到沙盒内（D:\\开发\\unified-rx-mcp\\_pytest_tmp）

解决：沙盒（UNIFIED_RX_SANDBOX=D:\\开发）拦截 C:\\Temp 下的 pytest 临时目录。
"""
import os
import tempfile

# pytest 的 tmp_path/tmp_path_factory 基目录改到沙盒内
_SANDBOX_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pytest_tmp")
os.makedirs(_SANDBOX_TMP, exist_ok=True)
tempfile.tempdir = _SANDBOX_TMP

# 让 pytest 的 basetemp 用沙盒内目录
def pytest_configure(config):
    config.option.basetemp = _SANDBOX_TMP
