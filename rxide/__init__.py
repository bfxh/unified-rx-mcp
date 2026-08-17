#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""RX-IDE Lite 后端纯逻辑包（零第三方依赖，全 stdlib）。

子模块：
  settings — 配置读写（~/.unified-rx/rxide.json）
  ai       — LLM 对话（urllib，支持 SSE 流式）
  commands — 对话行命令解析 / 上下文构建 / LLM 编辑提取
  diff     — 编辑应用 + 行级 diff
  termlog  — 内建命令执行 + 扫描日志尾部

导入即把项目根（rxide 上级目录）插入 sys.path——
`import server` / `from ide_commands import local_run` 等直达（同 ide_ui.py 做法）。
"""
import os
import sys

__version__ = "0.1.0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
