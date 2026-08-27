# -*- coding: utf-8 -*-
"""conftest.py —— pytest 全局配置：tmp 基目录在 %TEMP%\\unified-rx-pytest，
并把该前缀加入沙盒放行（保持 fail-closed 语义：仅此显式白名单 + 项目根）。

不再把 _pytest_tmp 放仓库根（UPGRADE-A2）：夹具残留会污染 bug_scan 自扫与 git。
"""
import os
import tempfile

# pytest 默认跑在 fail-closed 沙盒内：未设置时 = 项目根 + 专用 tmp 前缀
_TMP_BASE = os.path.join(tempfile.gettempdir(), "unified-rx-pytest")
os.makedirs(_TMP_BASE, exist_ok=True)
os.environ.setdefault("UNIFIED_RX_SANDBOX", os.pathsep.join([
    r"D:\开发",           # 真实工作区
    _TMP_BASE,            # pytest 夹具专用前缀（进程内显式授权）
]))
os.environ["UNIFIED_RX_SANDBOX"] = os.environ["UNIFIED_RX_SANDBOX"].replace(os.pathsep, ";")

tempfile.tempdir = _TMP_BASE


def pytest_configure(config):
    config.option.basetemp = _TMP_BASE
