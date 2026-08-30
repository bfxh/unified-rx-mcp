# -*- coding: utf-8 -*-
"""tools/ide_read.py —— 结构化读取（S66）：ide_outline / ide_read_symbol。

AST 级（复用 scan._func_spans，零 LSP 依赖、毫秒级）——document_symbols 要起
语言服务器，符号清单和函数精读这种高频动作不该付那个成本。
"""
import os

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.scan import _lang_of, _symbol_spans


def _load(file):
    real = _fs_resolve(file)
    if not os.path.isfile(real):
        return None, None, None
    lang = _lang_of(real)
    if not lang:
        return real, lang, None
    with open(real, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    return real, lang, src.split("\n")


@tool("ide_outline", "文件结构大纲：函数/方法/类型（struct/enum/trait/impl/class）"
      "清单——名称、起止行、参数数、种类——AST 级零依赖毫秒级，"
      "比 LSP document_symbols 适合高频调用", "ide",
      {"type": "object",
       "properties": {"file": {"type": "string", "description": "文件（沙盒内）"}},
       "required": ["file"]})
def ide_outline(file):
    real, lang, lines = _load(file)
    if lines is None:
        return {"error": f"文件不可读或非代码文件: {file}" if real else f"文件不存在: {file}"}
    spans = _symbol_spans(lines, lang) if lang else []
    symbols = []
    for n, i, e, p in spans[:300]:
        header = lines[i]
        params = header.count(",") + 1 if ("(" in header and ")" in header
                                           and p == "fn") else 0
        symbols.append({"name": n, "kind": p, "line": i + 1,
                        "end_line": e, "params": params})
    return {"file": real, "lang": lang, "total": len(symbols),
            "symbols": symbols}


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
    real, lang, lines = _load(file)
    if lines is None:
        return {"error": f"文件不可读或非代码文件: {file}" if real else f"文件不存在: {file}"}
    spans = [s for s in _symbol_spans(lines, lang) if s[0] == name] if lang else []
    if not spans:
        return {"error": f"符号 {name} 不存在——用 ide_outline 查清单"}
    if occurrence < 1 or occurrence > len(spans):
        return {"error": f"occurrence={occurrence} 越界（{name} 共 {len(spans)} 处）"}
    n, i, end, kind = spans[occurrence - 1]
    params = 0
    header = lines[i]
    if "(" in header and ")" in header:
        params = header.count(",") + 1
    return {"file": real, "lang": lang, "name": n, "kind": kind,
            "start": i + 1, "end": end, "lines": end - i, "params": params,
            "content": "\n".join(lines[i:end])}
