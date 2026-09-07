# -*- coding: utf-8 -*-
"""tools/ide_read.py —— 结构化读取（S66 立面 / S92 薄壳化）：ide_outline / ide_read_symbol。

S92 起整体转调 rx-ide.exe（唯一实现在 rust/src/ide.rs）：四语言符号行匹配器
（tools/scan.py::_symbol_spans 的 S70 语义）、300 符号上限、params 计数口径
（outline 只认 fn / read_symbol 只认括号）、沙盒 resolve 与文件级错误包络全部
在 exe 侧等价复刻；Python 侧只做 exe 定位与包络翻译。语义等价由 S92 对照实验
证明（43 场景 masked 全等，含 CRLF/幻影行/unicode 标识符/一行 fn 含 struct）。
零 LSP 依赖的 S66 立意不变：高频动作不付语言服务器成本。
"""
import json
import os
import subprocess

from registry import tool

_RX_IDE_EXE_NAME = "rx-ide.exe"


def _rx_ide_exe():
    """定位 rx-ide.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/scan.py::_rx_scan_exe 同纪律：候选必须是已存在且文件名恰为
    rx-ide.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_IDE_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_IDE_EXE_NAME:
            return c
    return None


def _rx_ide_call(argv):
    """薄壳转调 rx-ide.exe，返回结果 dict；resolve 层拒绝 raise ValueError。

    S90 二进制通道纪律：input 恒传（空 bytes 即 EOF），子进程绝不继承宿主的
    协议管道；stdout/stderr 按 utf-8/replace 手工解码。
    """
    exe = _rx_ide_exe()
    if not exe:
        raise ValueError("rx-ide.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    try:
        cp = subprocess.run([exe] + argv, capture_output=True, timeout=120,
                            input=b"")
    except subprocess.TimeoutExpired:
        raise ValueError("rx-ide 超时（120s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-ide 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-ide 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # resolve 层拒绝（沙盒越界/未配置/path 必填）→ 与旧实现同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-ide 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("ide_outline", "文件结构大纲：函数/方法/类型（struct/enum/trait/impl/class）"
      "清单——名称、起止行、参数数、种类——AST 级零依赖毫秒级，"
      "比 LSP document_symbols 适合高频调用", "ide",
      {"type": "object",
       "properties": {"file": {"type": "string", "description": "文件（沙盒内）"}},
       "required": ["file"]})
def ide_outline(file):
    return _rx_ide_call(["outline", file])


@tool("ide_read_symbol", "按名读符号完整身体（函数/struct/enum/trait/impl/class）"
      "——定位+精读一步到位，不再靠 locate_edit 行号 + code_context 半径窗口拼凑",
      "ide",
      {"type": "object",
       "properties": {
           "file": {"type": "string", "description": "文件（沙盒内）"},
           "name": {"type": "string", "description": "符号名（精确匹配）"},
           "occurrence": {"type": "integer",
                          "description": "同名第几次出现（默认 1）"},
       },
       "required": ["file", "name"]})
def ide_read_symbol(file, name, occurrence=1):
    return _rx_ide_call(["read_symbol", file, name, str(int(occurrence))])
