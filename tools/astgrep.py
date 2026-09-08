# -*- coding: utf-8 -*-
"""tools/astgrep.py —— ast-grep 结构搜索（S111，可选外部引擎）薄壳。

实现在 rust/src/astgrep.rs + rx-scan astgrep 子命令（探测 PATH 上的 ast-grep/sg，
argv 直调无 shell；未装 → 清晰报错 + 安装提示，不静默降级）。Python 侧只做：
pattern 字符白名单校验 + 沙盒 resolve + 转调 exe（无 subprocess，符合本仓
"重活走 exe"纪律）。
"""
import os

from registry import tool
from tools.fs import _resolve as _fs_resolve

_PATTERN_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t\n\r$(){}[];,.:<>=+-*/%!&|?'\"`~#@^_\\"
)


@tool("ast_grep", "结构搜索（可选外部引擎 ast-grep）：模式即代码（$VAR 通配）——"
      "如 `console.log($A)`、`if ($C) { $$$ }`；未安装时清晰报错并给安装提示",
      "scan",
      {"type": "object",
       "properties": {
           "pattern": {"type": "string", "description": "结构模式（$VAR 单节点 / $$$ 多节点）"},
           "path": {"type": "string", "description": "文件或目录（沙盒内）"},
           "k": {"type": "integer", "description": "最多返回条数（默认 50）"},
       },
       "required": ["pattern", "path"]})
def ast_grep(pattern, path, k=50):
    if not isinstance(pattern, str) or not pattern or len(pattern) > 4096:
        return {"error": "pattern 必填且 ≤4096 字符"}
    if not set(pattern) <= _PATTERN_OK:
        return {"error": "pattern 含非法字符（仅接受可打印 ASCII 与换行/制表）"}
    try:
        path = _fs_resolve(path)      # S88：读路径过沙盒
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    from tools.scan import _rx_scan_call
    return _rx_scan_call(["astgrep", pattern, path, str(int(k))])
