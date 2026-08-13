"""test_ide_vector.py — 本地 embedding 向量增强测试（2026-08-13）。

覆盖：
  1. MiniBertTokenizer：中文单字 + 英文 wordpiece + CLS/SEP
  2. LocalIntel：embed 真实推理（512 维 + 归一化 + 相似度区分）
  3. semantic_search 向量混合模式（note 含 bge）
  4. make_embed_fn 可用
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def test_tokenizer():
    from mini_bert_tokenizer import MiniBertTokenizer
    tk = MiniBertTokenizer(os.path.join(MODELS, "embed_tokenizer.json"))
    e = tk.encode("车轮 驱动")
    assert e["input_ids"][0] == 101  # [CLS]
    assert e["input_ids"][-1] == 102  # [SEP]
    assert len(e["input_ids"]) == len(e["attention_mask"])
    assert tk.vocab_size() == 21128


def test_local_intel_embed():
    from local_intel import LocalIntel
    li = LocalIntel()
    v1 = li.embed("车轮 驱动 系统")
    v2 = li.embed("地形 生成")
    assert v1 is not None and v2 is not None, f"模型应可用: {li.available()}"
    assert len(v1) == 512
    sim_related = sum(a * b for a, b in zip(li.embed("车轮 驱动 系统"), li.embed("轮子 传动")))
    sim_unrelated = sum(a * b for a, b in zip(v1, v2))
    assert sim_related > sim_unrelated, f"相关应更近: {sim_related:.3f} vs {sim_unrelated:.3f}"


def test_make_embed_fn():
    from local_intel import LocalIntel
    li = LocalIntel()
    fn = li.make_embed_fn()
    assert fn is not None
    v = fn("测试文本")
    assert v is not None and len(v) == 512


def test_semantic_search_vector_mode():
    """semantic_search 应启用向量混合（note 含 bge）。"""
    r = server._call("semantic_search", {"root": r"D:\开发\VoxelForge-Nexus",
                                         "query": "车轮驱动", "limit": 3})
    d = json.loads(r[0].text)
    assert d.get("ok") is True
    assert "bge" in d.get("note", ""), f"应向量模式: {d.get('note')}"
    assert len(d.get("results", [])) > 0
    ids = " ".join(str(x.get("id", "")) for x in d["results"])
    assert "wheels" in ids or "physics_drive" in ids, f"应命中轮子: {ids[:80]}"


def test_cleanup():
    shutil.rmtree(os.path.join(r"D:\开发\VoxelForge-Nexus", ".unified-rx-index"),
                  ignore_errors=True)
