# -*- coding: utf-8 -*-
"""tools/astscan.py —— S9 结构化扫描域：ast_scan（S84 起薄壳）

方法论（用户规则 2026-08-27）：分析按算法、按小的来再大的来，结论趋向于最小单元。
文本正则层（bug_scan）只是线索流；本工具是结构化层：
- Python：真语法树节点级判定调用（Call），天然免疫注释/字符串干扰；
- JS：词法掩码 → 括号平衡调用面 → 分类（成员链 X.exec() 排除，new Function 显式命中）；
- Rust：词法掩码 → 结构化信号（unwrap/unsafe/panic）+ 跨文件可达性归档。

S84（2026-09-05）：全量原生化（rust/src/astscan.rs + pyast.rs 手写迷你解析器，
rx-scan astscan 子命令）——Python 侧只留薄壳转调，单行 JSON 透传；
exe 缺失报清晰错误不静默降级。迁移前已按 14 组语料与旧实现逐字节对照一致。
"""
import json
import os
import subprocess

from registry import tool

_RX_EXE_NAME = "rx-scan.exe"


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/fs.py::_rx_fs_exe 同纪律：候选必须是已存在且文件名恰为
    rx-scan.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_EXE_NAME:
            return c
    return None


def _rx_scan_call(path, max_files):
    """薄壳转调 rx-scan.exe astscan，返回结果 dict；用法级拒绝 raise ValueError。

    退出码契约：0 = 工具级结果（含 {"error": ...} 包络，原样透传）；
    2 = 用法级拒绝 → ValueError；其他非零 = 执行失败 → ValueError。
    """
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, "astscan", path, str(max_files)]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan 超时（600s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-scan 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("ast_scan", "结构化扫描（S9）：Python 真 AST / JS 词法掩码+括号平衡 / Rust 结构化信号；输出最小单元条目，先小后大", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录"},
           "max_files": {"type": "integer", "description": "上限（默认 200）"},
       },
       "required": ["path"]})
def ast_scan(path, max_files=200):
    return _rx_scan_call(path, max_files)
