#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""cost_core.py — 成本核算核心：token 估算 + 单价表 + 汇总（2026-08-17）。

用户要求（2026-08-17）："每一个代码、每一个工具调用次数和 token 消耗成本都是要算的"。

- estimate_tokens：混合近似（英文 4 字符≈1 token，中文 1 字≈1 token）
- MODEL_PRICES：常见模型单价表（$ / 1M tokens，输入/输出）
- estimate_cost：token → 美元 + 人民币（汇率可配）
- summarize：调用记录 → 按工具/按天/按项目汇总（次数/token/成本）
"""
import datetime
import re
import time

# 模型单价（美元 / 1M tokens）：(input, output)
# 参考公开定价（2026-08）：DeepSeek V3.1 / Claude 4 / GPT-4o / Qwen-Max
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),      # DeepSeek V3.1 标准
    "deepseek-reasoner": (0.55, 2.19),  # DeepSeek R1/推理
    "claude-sonnet": (3.00, 15.00),     # Claude Sonnet 4
    "claude-opus": (15.00, 75.00),      # Claude Opus 4
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "qwen-max": (1.60, 6.40),
    "qwen-plus": (0.40, 1.20),
    "default": (1.00, 4.00),            # 通用保守价
}

_CN_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """token 近似：中文按字（1 字≈1 token），其余按 4 字符≈1 token。"""
    if not text:
        return 0
    text = text[:200_000]  # 防超长文本拖垮（上限 200k 字符）
    cn = len(_CN_RE.findall(text))
    other = len(text) - cn
    return cn + max(1, other // 4)


def estimate_cost(in_tokens: int, out_tokens: int,
                  model: str = "deepseek-chat",
                  usd_cny: float = 7.2) -> dict:
    """token → 成本（美元 + 人民币）。model 不存在时回落 default。"""
    price_in, price_out = MODEL_PRICES.get(model, MODEL_PRICES["default"])
    usd = in_tokens * price_in / 1_000_000 + out_tokens * price_out / 1_000_000
    return {
        "model": model,
        "tokens_in": in_tokens,
        "tokens_out": out_tokens,
        "tokens_total": in_tokens + out_tokens,
        "cost_usd": round(usd, 6),
        "cost_cny": round(usd * usd_cny, 4),
        "price_in_per_1m": price_in,
        "price_out_per_1m": price_out,
    }


def summarize(records: list[dict], model: str = "deepseek-chat",
              usd_cny: float = 7.2) -> dict:
    """调用记录 → 汇总（按工具 / 按天 / 按项目；次数 + token + 成本）。"""
    by_tool: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    total_in = total_out = total_calls = total_ms = 0

    for r in records:
        tool = str(r.get("tool") or r.get("action") or "?")
        project = str(r.get("task") or r.get("project") or "unified-rx")
        ts = r.get("ts") or time.time()
        try:
            day = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            day = "unknown"
        tin = int(r.get("tokens_in") or 0)
        tout = int(r.get("tokens_out") or 0)
        ms = float(r.get("duration_ms") or 0)

        total_calls += 1
        total_in += tin
        total_out += tout
        total_ms += ms

        for bucket, key in ((by_tool, tool), (by_day, day), (by_project, project)):
            b = bucket.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                        "duration_ms": 0.0})
            b["calls"] += 1
            b["tokens_in"] += tin
            b["tokens_out"] += tout
            b["duration_ms"] += ms

    cost = estimate_cost(total_in, total_out, model, usd_cny)
    return {
        "ok": True,
        "model": cost["model"],
        "totals": {
            "calls": total_calls,
            "tokens_in": total_in,
            "tokens_out": total_out,
            "tokens_total": cost["tokens_total"],
            "duration_ms": round(total_ms, 1),
            "cost_usd": cost["cost_usd"],
            "cost_cny": cost["cost_cny"],
        },
        "by_tool": _bucket_sorted(by_tool),
        "by_day": _bucket_sorted(by_day),
        "by_project": _bucket_sorted(by_project),
    }


def _bucket_sorted(bucket: dict) -> list[dict]:
    out = []
    for key, b in bucket.items():
        c = estimate_cost(b["tokens_in"], b["tokens_out"])
        out.append({"key": key, "calls": b["calls"],
                    "tokens_in": b["tokens_in"], "tokens_out": b["tokens_out"],
                    "duration_ms": round(b["duration_ms"], 1),
                    "cost_usd": c["cost_usd"], "cost_cny": c["cost_cny"]})
    out.sort(key=lambda x: -x["calls"])
    return out


def code_cost(code: str, model: str = "deepseek-chat") -> dict:
    """一段代码的 token/成本估算（写代码成本核算）。"""
    tok = estimate_tokens(code)
    return estimate_cost(tok, 0, model)


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(code_cost(open(sys.argv[1], encoding="utf-8").read()),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(estimate_cost(1000, 500, "deepseek-chat"), indent=2))
