#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""mini_bert_tokenizer.py — 轻量 BERT WordPiece tokenizer（bge-small-zh ONNX 推理用）。

读 HuggingFace tokenizer.json（Xenova/transformers.js 格式）：
  - BertNormalizer（clean_text + handle_chinese_chars + lowercase）
  - BertPreTokenizer（空白/标点切分；中文按单字）
  - WordPiece（## 子词）+ CLS/SEP 拼接 + max_len 截断
纯 Python 零依赖（re + json）。~200 行。
"""

import json
import re
import unicodedata

# BertNormalizer 的 clean_text 规则
_CLEAN_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# handle_chinese_chars：中文字符两侧加空格
_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
# BertPreTokenizer：空白分隔 + 标点剥离
_PUNCT_RE = re.compile(r"([\s.,!?;:()\[\]{}<>\"'`~@#$%^&*_+=\-/\\|]+)")


class MiniBertTokenizer:
    """简化 BERT tokenizer（推理够用：编码为 input_ids + attention_mask）。"""

    def __init__(self, tokenizer_json: str, max_len: int = 256):
        with open(tokenizer_json, encoding="utf-8") as f:
            data = json.load(f)
        model = data["model"]
        self.vocab: dict[str, int] = model["vocab"]
        self.unk_id = int(self.vocab.get(model.get("unk_token", "[UNK]"), 100))
        self.pad_id = int(self.vocab.get("[PAD]", 0))
        self.cls_id = int(self.vocab.get("[CLS]", 101))
        self.sep_id = int(self.vocab.get("[SEP]", 102))
        self.continuing_prefix = model.get("continuing_subword_prefix", "##")
        self.max_len = max_len
        # 中文单字加速集
        self._cjk_chars = set(
            re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", "".join(self.vocab.keys())))

    # ── normalizer + pre_tokenizer ──
    def _normalize(self, text: str) -> str:
        text = _CLEAN_RE.sub(" ", text)
        text = unicodedata.normalize("NFC", text)
        # handle_chinese_chars：CJK 前后加空格（让 pre_tokenizer 拆成单字）
        text = _CJK_RE.sub(r" \1 ", text)
        return text

    def _pretokenize(self, text: str) -> list[str]:
        """BertPreTokenizer：按空白/标点切分，去空。"""
        parts = [p for p in _PUNCT_RE.split(text) if p and not p.isspace()]
        return parts

    # ── wordpiece ──
    def _wordpiece(self, word: str) -> list[str]:
        if word in self.vocab:
            return [word]
        tokens = []
        start = 0
        wlen = len(word)
        while start < wlen:
            end = wlen
            cur = None
            while end > start:
                sub = word[start:end]
                if start > 0:
                    sub = self.continuing_prefix + sub
                if sub in self.vocab:
                    cur = sub
                    break
                end -= 1
            if cur is None:
                return ["[UNK]"]  # 符号占位（encode 时映射 id）
            tokens.append(cur)
            start = end
        return tokens

    # ── encode ──
    def encode(self, text: str, max_len: int | None = None) -> dict[str, list[int]]:
        """text → {input_ids, attention_mask}。bge 有 max_len 截断。"""
        max_len = max_len or self.max_len
        ids: list[int] = [self.cls_id]
        for word in self._pretokenize(self._normalize(text)):
            for tok in self._wordpiece(word):
                tid = self.vocab.get(tok, self.unk_id)
                ids.append(tid)
                if len(ids) >= max_len - 1:
                    break
            if len(ids) >= max_len - 1:
                break
        ids.append(self.sep_id)
        ids = ids[:max_len]
        mask = [1] * len(ids)
        return {"input_ids": ids, "attention_mask": mask}

    def vocab_size(self) -> int:
        return len(self.vocab)


def encode_batch(tokenizer: MiniBertTokenizer, texts: list[str],
                 max_len: int = 256) -> dict[str, list[list[int]]]:
    """批量编码（batch → padding 到 batch 内最长，pad_id 补位）。"""
    encs = [tokenizer.encode(t, max_len=max_len) for t in texts]
    max_t = max(len(e["input_ids"]) for e in encs)
    input_ids = [e["input_ids"] + [tokenizer.pad_id] * (max_t - len(e["input_ids"])) for e in encs]
    masks = [e["attention_mask"] + [0] * (max_t - len(e["attention_mask"])) for e in encs]
    return {"input_ids": input_ids, "attention_mask": masks}
