# -*- coding: utf-8 -*-
"""tools/scip.py —— SCIP 索引消费（S112，ADVANCES P2 第 11 项）。

SCIP（sourcegraph/scip）是紧凑的代码情报索引格式（protobuf）：外部索引器
（`rust-analyzer scip .`、scip-python、scip-java…）产出后，本工具**只读**消费——
给出某符号的定义/引用位置，**不用起 LSP 会话**。

实现：手写 protobuf wire 解析（varint + 长度前缀字段），零依赖（本仓已有手写
sha256/JSON 先例）。取用到的字段：
- Index.documents = 2；Document.relative_path = 1、Document.occurrences = 2；
- Occurrence.range = 1（packed int32，[startLine, startChar, ...] 0 基）、
  Occurrence.symbol = 2（字符串）、Occurrence.symbol_roles = 3（1=Definition）。

诚实边界：不生成索引（需外部索引器）；不解释 symbol 的完整语法（按子串/精确
匹配查询）；索引新鲜度由生成方负责。
"""
import os

from registry import tool
from tools.fs import _resolve as _fs_resolve

_MAX_INDEX_BYTES = 512 * 1024 * 1024


def _varint(buf, i):
    shift = 0
    val = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 越界")


def _fields(buf):
    """迭代 (field_no, wire_type, payload)：wire0=值(int)，wire2=bytes，wire1/5=跳过。"""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fno, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
            yield fno, 0, val
        elif wire == 2:
            ln, i = _varint(buf, i)
            if i + ln > n:
                raise ValueError("长度越界")
            yield fno, 2, buf[i:i + ln]
            i += ln
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"未知 wire type {wire}")


def parse_index(data):
    """→ [{path, occurrences: [(line0, symbol, is_def)]}]（只取用到的字段）。"""
    docs = []
    for fno, wire, payload in _fields(data):
        if fno != 2 or wire != 2:
            continue
        path = ""
        occs = []
        for dfno, dwire, dpayload in _fields(payload):
            if dfno == 1 and dwire == 2:
                path = dpayload.decode("utf-8", "replace")
            elif dfno == 2 and dwire == 2:
                line0, symbol, is_def = -1, "", False
                for ofno, owire, opayload in _fields(dpayload):
                    if ofno == 1 and owire == 2:          # packed range
                        j = 0
                        ints = []
                        while j < len(opayload):
                            v, j = _varint(opayload, j)
                            ints.append(v)
                        if ints:
                            line0 = ints[0]
                    elif ofno == 1 and owire == 0:        # unpacked range
                        if line0 < 0:
                            line0 = opayload
                    elif ofno == 2 and owire == 2:
                        symbol = opayload.decode("utf-8", "replace")
                    elif ofno == 3 and owire == 0:
                        is_def = bool(opayload & 1)
                if symbol:
                    occs.append((line0, symbol, is_def))
        docs.append({"path": path, "occurrences": occs})
    return docs


@tool("scip_refs", "SCIP 索引消费（只读）：查符号的定义/引用位置——需要外部索引器"
      "产物（如 `rust-analyzer scip .`）；不起 LSP 会话", "ide",
      {"type": "object",
       "properties": {
           "index_file": {"type": "string", "description": "SCIP 索引文件（沙盒内）"},
           "symbol": {"type": "string",
                      "description": "符号（SCIP symbol 或其子串，如 `_resolve`）"},
           "k": {"type": "integer", "description": "最多返回文件数（默认 50）"},
       },
       "required": ["index_file", "symbol"]})
def scip_refs(index_file, symbol, k=50):
    if not isinstance(symbol, str) or not symbol:
        return {"error": "symbol 必填"}
    try:
        index_file = _fs_resolve(index_file)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(index_file):
        return {"error": f"不是文件或不存在: {index_file}"}
    try:
        size = os.path.getsize(index_file)
    except OSError as e:
        return {"error": f"读取失败: {e}"}
    if size > _MAX_INDEX_BYTES:
        return {"error": f"索引过大（{size // (1024 * 1024)}MB > 512MB）"}
    try:
        with open(index_file, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"error": f"读取失败: {e}"}
    try:
        docs = parse_index(data)
    except ValueError as e:
        return {"error": f"SCIP 解析失败: {e}（确认是 scip 索引而非 LSIF/其他格式）"}
    files = []
    total = 0
    for d in docs:
        lines = []
        ndef = 0
        for line0, sym, is_def in d["occurrences"]:
            if sym == symbol or symbol in sym:
                if line0 >= 0:
                    lines.append(line0 + 1)
                ndef += 1 if is_def else 0
        if lines:
            total += len(lines)
            files.append({"file": d["path"], "refs": len(lines),
                          "lines": sorted(set(lines))[:20], "defs": ndef})
        if len(files) >= int(k):
            break
    return {"engine": "scip", "index": index_file, "symbol": symbol,
            "total_refs": total, "files": files, "documents": len(docs),
            "note": "SCIP 索引由外部索引器生成（rust-analyzer scip . / scip-python 等）；"
                    "本工具只读不生成，索引新鲜度由生成方负责"}
