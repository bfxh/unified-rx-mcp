#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""local_intel.py — P3 本地智能推理层（抄 onnxruntime 部署思路）。

消费 distill_pipeline.py 产出的 ONNX 模型：
  - embed_model.onnx   代码语义嵌入（→ search_index 的 embed_fn，启用 RRF 混合检索）
  - error_model.onnx   错误分类（→ quality_scan 增强）
  - value_model.onnx   值函数（→ LATS 探索引擎用）

模型缺失时全部降级（返回 None / 提示），绝不炸。
"""
import os
from pathlib import Path

_MODELS_DIR = Path(__file__).resolve().parent / "models"


class LocalIntel:
    """本地模型推理（onnxruntime），模型缺失自动降级。"""

    def __init__(self, models_dir: Path | None = None):
        self._dir = Path(models_dir) if models_dir else _MODELS_DIR
        self._sessions: dict[str, object] = {}
        self._loaded: dict[str, bool] = {}

    # ── 会话管理 ──────────────────────────────────────────
    def _session(self, name: str):
        """加载 ONNX 会话（懒加载 + 缓存）。"""
        if name in self._sessions:
            return self._sessions[name]
        path = self._dir / f"{name}.onnx"
        if not path.exists():
            self._loaded[name] = False
            return None
        try:
            import onnxruntime as ort
            self._sessions[name] = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"])
            self._loaded[name] = True
            return self._sessions[name]
        except Exception:
            self._loaded[name] = False
            return None

    def available(self) -> dict[str, bool]:
        """各模型可用性。"""
        return {
            "embed": self._session("embed_model") is not None,
            "error": self._session("error_model") is not None,
            "value": self._session("value_model") is not None,
        }

    # ── 嵌入（→ search_index.embed_fn）────────────────────
    def embed(self, text: str) -> list[float] | None:
        """文本 → 512 维向量（bge-small-zh 真实推理）。模型缺失返回 None（调用方降级纯 BM25）。

        2026-08-13：接入真实 ONNX 模型（Xenova/bge-small-zh-v1.5，CPU 推理 ~10ms）。
        """
        sess = self._session("embed_model")
        if sess is None:
            return None
        try:
            import numpy as np
            from mini_bert_tokenizer import MiniBertTokenizer, encode_batch
            tok_path = self._dir / "embed_tokenizer.json"
            if not tok_path.exists():
                return None
            tok = MiniBertTokenizer(str(tok_path))
            b = encode_batch(tok, [text], max_len=128)
            feeds = {k: np.array(v, dtype=np.int64) for k, v in b.items()}
            feeds["token_type_ids"] = np.zeros_like(feeds["input_ids"])
            out = sess.run(None, feeds)[0]
            vec = out[0, 0, :].astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return [float(x) for x in vec[:512]]
        except Exception:
            return None

    # ── 错误分类（→ quality 增强）─────────────────────────
    def classify_error(self, text: str) -> dict | None:
        """错误文本 → {has_issue, confidence}。模型缺失返回 None。"""
        sess = self._session("error_model")
        if sess is None:
            return None
        try:
            import numpy as np
            input_name = sess.get_inputs()[0].name
            out = sess.run(None, {input_name: np.array([text], dtype=object)})[0]
            prob = float(out[0][1]) if out.shape[-1] > 1 else float(out[0][0])
            return {"has_issue": prob > 0.5, "confidence": round(prob, 4)}
        except Exception:
            return None

    # ── 值函数（→ LATS 探索）──────────────────────────────
    def value(self, state: str) -> float | None:
        """状态 → 值估计 [0,1]。模型缺失返回 None。"""
        sess = self._session("value_model")
        if sess is None:
            return None
        try:
            import numpy as np
            input_name = sess.get_inputs()[0].name
            out = sess.run(None, {input_name: np.array([state], dtype=object)})[0]
            return float(np.clip(out[0], 0.0, 1.0))
        except Exception:
            return None

    # ── 便捷 ──────────────────────────────────────────────
    def make_embed_fn(self):
        """返回适配 search_index.search_hybrid 的 embed_fn（模型缺失返回 None）。"""
        if self._session("embed_model") is None:
            return None
        return self.embed
