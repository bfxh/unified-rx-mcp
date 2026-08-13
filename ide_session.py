#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_session.py — 编辑会话模型（IDE_ENHANCE_PLAN R5，抄 AetherStudio）。

  FastLineIndex — 行号 ↔ 字节偏移 O(log n) 转换（升 lsp_position_convert；
                  大文件 5000+ 行编辑场景每次定位不用全文件扫行）
  PieceTable   — 增量编辑文档模型：original + add buffers + 编辑列表，
                  只算增量 diff 不重写全文；piece 合并防碎片

两者纯 Python 无依赖（可独立测试）。
"""

import bisect
import itertools
from dataclasses import dataclass, field


# ── FastLineIndex ──────────────────────────────────────────
class FastLineIndex:
    """行起始偏移索引：offsets[i] = 第 i 行（0-based）起始字节偏移。

    构建 O(n)，行号/偏移互转 O(log n)。大文件（5000+ 行）优于逐行扫描。
    """

    def __init__(self, text: str):
        self.text = text
        self._offsets = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._offsets.append(i + 1)

    @property
    def line_count(self) -> int:
        return len(self._offsets)

    def line_start(self, line: int) -> int:
        """行起始偏移（越界返回文本末尾）。"""
        if line < 0:
            return 0
        if line >= self.line_count:
            return len(self.text)
        return self._offsets[line]

    def line_end(self, line: int) -> int:
        """行结束偏移（不含换行符）。"""
        if line < 0 or line >= self.line_count:
            return len(self.text)
        nxt = self._offsets[line + 1] if line + 1 < self.line_count else len(self.text)
        end = nxt - 1
        if end < 0:
            return 0  # 空文本：行 0 无内容
        return end if self.text[end:end + 1] != "\n" else nxt

    def position_to_offset(self, line: int, col: int) -> int:
        """(行,列) → 字节偏移。col 按字符（code point）计。"""
        start = self.line_start(line)
        return start + min(col, self.line_end(line) - start)

    def offset_to_position(self, offset: int) -> tuple[int, int]:
        """字节偏移 → (行, 列)。二分查找行。"""
        offset = max(0, min(offset, len(self.text)))
        i = bisect.bisect_right(self._offsets, offset) - 1
        return i, offset - self._offsets[i]


# ── PieceTable ─────────────────────────────────────────────
@dataclass
class _Piece:
    """一段文本：source（0=原始, 1+=追加缓冲）+ 起止偏移。"""
    source: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class PieceTable:
    """增量编辑文档模型（AetherStudio 同款思想，纯 Python 简化版）。

    insert/delete 只追加编辑记录（O(1) 摊销），text() 时按 piece 重建。
    编辑后行索引变化通过 FastLineIndex(text()) 即时重建。
    """

    original: str = ""
    _buffers: list[str] = field(default_factory=list)
    _pieces: list[_Piece] = field(default_factory=list)

    def __post_init__(self):
        if self.original:
            self._pieces = [_Piece(0, 0, len(self.original))]

    # ── 编辑操作 ──
    def insert(self, offset: int, text: str) -> None:
        if not text:
            return
        self._buffers.append(text)
        src = len(self._buffers)  # buffer 索引（1-based；0=original）
        self._insert_piece(offset, _Piece(src, 0, len(text)))

    def delete(self, start: int, end: int) -> None:
        if end <= start:
            return
        self._delete_range(start, end)

    def replace(self, start: int, end: int, text: str) -> None:
        self._delete_range(start, end)
        if text:
            self.insert(start, text)

    # ── 内部：piece 切分/插入/删除 ──
    def _split_at(self, offset: int) -> int:
        """在 offset 处切分 piece 边界，返回 piece 索引（offset 恰在边界则返回该索引）。"""
        cur = 0
        for i, p in enumerate(self._pieces):
            if cur <= offset <= cur + p.length:
                if offset > cur and offset < cur + p.length:
                    # 切分
                    cut = offset - cur
                    left = _Piece(p.source, p.start, p.start + cut)
                    right = _Piece(p.source, p.start + cut, p.end)
                    self._pieces[i:i + 1] = [left, right]
                    return i + 1
                return i if offset == cur else i + 1
            cur += p.length
        return len(self._pieces)

    def _insert_piece(self, offset: int, piece: _Piece) -> None:
        idx = self._split_at(offset)
        self._pieces.insert(idx, piece)
        self._merge_adjacent(idx)

    def _delete_range(self, start: int, end: int) -> None:
        left = self._split_at(start)
        right = self._split_at(end)
        del self._pieces[left:right]

    def _merge_adjacent(self, idx: int) -> None:
        """合并相邻同源 piece（防碎片）。"""
        i = max(0, idx - 1)
        while i + 1 < len(self._pieces):
            a, b = self._pieces[i], self._pieces[i + 1]
            if a.source == b.source and a.end == b.start:
                self._pieces[i] = _Piece(a.source, a.start, b.end)
                del self._pieces[i + 1]
            else:
                i += 1

    # ── 读取 ──
    def _piece_text(self, p: _Piece) -> str:
        buf = self.original if p.source == 0 else self._buffers[p.source - 1]
        return buf[p.start:p.end]

    def text(self) -> str:
        return "".join(self._piece_text(p) for p in self._pieces)

    def length(self) -> int:
        return sum(p.length for p in self._pieces)

    def line_index(self) -> FastLineIndex:
        return FastLineIndex(self.text())

    def edit_count(self) -> int:
        return len(self._buffers)  # 追加缓冲数 ≈ 编辑批次数
