#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""distill_pipeline.py — P3 蒸馏管线（抄 unsloth/onnxruntime 思路，TOP_TIER_PLAN ⑦）。

目标：蒸馏 3 个本地小模型（代码语义嵌入 / 错误分类 / 值函数），
教师=大模型 API 或本地大模型，学生=小模型（Qwen2.5-1.5B 级 / ModernBERT），
导出 ONNX + INT8 量化 → onnxruntime 推理（local_intel.py 消费）。

⚠️ 需要 GPU + 训练依赖（torch/transformers/unsloth）：
  pip install torch transformers unsloth onnx onnxruntime
数据自举：从 unified-rx 历史数据生成（scan-log / 教训库 / stats 调用历史）。

用法：
  python distill_pipeline.py --stage prepare   # 1. 生成训练数据（自举）
  python distill_pipeline.py --stage distill   # 2. 蒸馏训练（教师→学生）
  python distill_pipeline.py --stage export    # 3. 导出 ONNX + 量化
  python distill_pipeline.py --all             # 全流程
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 数据源（unified-rx 历史数据）
_SCAN_LOG = Path(os.path.expanduser("~/.unified-rx/scan-log.jsonl"))
_LSE_STATE = Path(os.path.expanduser("~/.unified-rx/lse-state.json"))
_OUT_DIR = Path(__file__).resolve().parent / "models"


# ── Stage 1: 数据准备（自举）──────────────────────────────
def prepare_data(out_dir: Path) -> dict:
    """从 scan-log / lse-state 生成蒸馏训练数据。

    三类数据：
      1. 代码语义嵌入：代码片段（file:line + 内容）→ 语义对（相似/不相似）
      2. 错误分类：scan-log 的错误摘要 → 类别标签
      3. 值函数：教训 utility + 上下文 → (state, value) 对
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"scan_rows": 0, "lesson_rows": 0, "embed_pairs": 0,
             "error_rows": 0, "value_rows": 0}

    embed_pairs, error_rows, value_rows = [], [], []

    # 1. scan-log（扫描记录：工具/摘要/路径）
    if _SCAN_LOG.exists():
        with open(_SCAN_LOG, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats["scan_rows"] += 1
                summary = str(rec.get("summary", ""))[:200]
                tool = rec.get("tool", "")
                if not summary:
                    continue
                # 错误分类样本：工具 + 摘要 → 是否含问题
                has_issue = any(k in summary for k in
                                ("issue", "错误", "warning", "bug", "问题"))
                error_rows.append({"text": f"[{tool}] {summary}",
                                   "label": 1 if has_issue else 0})
                # 值函数样本：教训 utility 关联
                if has_issue:
                    value_rows.append({"state": summary, "value": 0.2})
                else:
                    value_rows.append({"state": summary, "value": 0.8})

    # 2. lse-state（教训 utility）
    if _LSE_STATE.exists():
        try:
            with open(_LSE_STATE, encoding="utf-8") as fh:
                lse = json.load(fh)
            lessons = lse.get("lessons", {})
            if isinstance(lessons, dict):
                for lid, lesson in lessons.items():
                    stats["lesson_rows"] += 1
                    utility = float(lesson.get("utility", 0.5)) if isinstance(lesson, dict) else 0.5
                    value_rows.append({"state": str(lid)[:100], "value": utility})
        except (json.JSONDecodeError, OSError):
            pass

    # 3. 嵌入对（从错误样本构造相似/不相似对）
    texts = [e["text"] for e in error_rows[:2000]]
    for i in range(0, len(texts) - 1, 2):
        if i + 1 < len(texts):
            embed_pairs.append({"anchor": texts[i], "positive": texts[i + 1],
                                "label": 1})  # 同源近似（简化；真实需语义标注）
    stats["embed_pairs"] = len(embed_pairs)
    stats["error_rows"] = len(error_rows)
    stats["value_rows"] = len(value_rows)

    # 落盘
    (out_dir / "embed_pairs.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in embed_pairs[:5000]),
        encoding="utf-8")
    (out_dir / "error_rows.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in error_rows[:5000]),
        encoding="utf-8")
    (out_dir / "value_rows.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in value_rows[:5000]),
        encoding="utf-8")
    return stats


# ── Stage 2/3: 蒸馏训练 + 导出（占位实现，需 GPU 环境）────
def distill(out_dir: Path) -> dict:
    """蒸馏训练（教师→学生）。

    完整实现需 torch/transformers/unsloth + GPU。此处提供结构：
      - 教师：大模型 API 或本地（生成软标签）
      - 学生：ModernBERT/Qwen2.5-1.5B（LoRA 微调）
      - 损失：KL 散度（蒸馏）+ CE（硬标签）
    环境未就绪时返回指导信息（不炸）。
    """
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return {"ok": False, "stage": "distill",
                "error": "训练依赖未装：pip install torch transformers unsloth",
                "guide": "GPU 环境就绪后运行；数据已由 prepare 生成在 models/"}
    # ---- 完整实现骨架（GPU 环境执行）----
    # from unsloth import FastLanguageModel
    # model, tokenizer = FastLanguageModel.from_pretrained("Qwen/Qwen2.5-1.5B")
    # ... LoRA 训练（数据集 = models/*.jsonl）
    # ... 蒸馏损失 = KL(student_logits, teacher_logits) + CE
    return {"ok": False, "stage": "distill",
            "error": "蒸馏训练需 GPU + 数据标注流程（骨架已就位，见代码注释）"}


def export_onnx(out_dir: Path) -> dict:
    """导出 ONNX + INT8 量化（onnxruntime 消费）。"""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return {"ok": False, "stage": "export",
                "error": "onnxruntime 未装：pip install onnxruntime"}
    return {"ok": False, "stage": "export",
            "error": "需先完成 distill 得到学生模型权重；导出用 transformers.onnx 或 optimum"}


def main():
    ap = argparse.ArgumentParser(description="蒸馏管线（P3 本地智能）")
    ap.add_argument("--stage", choices=["prepare", "distill", "export"],
                    default="prepare")
    ap.add_argument("--out", default=str(_OUT_DIR))
    args = ap.parse_args()
    out = Path(args.out)
    if args.stage == "prepare":
        stats = prepare_data(out)
        print(json.dumps({"ok": True, "stage": "prepare", "stats": stats},
                         ensure_ascii=False, indent=2))
    elif args.stage == "distill":
        print(json.dumps(distill(out), ensure_ascii=False, indent=2))
    elif args.stage == "export":
        print(json.dumps(export_onnx(out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
