#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""speculate —— 推测执行（2026-08-15，阶段3）。

预测下一步工具调用 → 预执行（仅幂等只读白名单）→ 结果进 ide_cache →
用户实际调用时缓存命中秒回。

规则（安全边界）：
- 白名单：仅幂等只读工具（bug_scan/std_check/cb_scan/locate_edit/
  game_check/ide_references/cb_status）
- 单次最多 SPECULATE_MAX（默认 3）个预测
- 预测键 = 工具名+参数签名——与实际调用命中才复用（防错缓存）
- 绝不预执行写操作/有副作用调用
"""
import hashlib
import json
import os
import threading
import time


SPECULATE_WHITELIST = {
    "bug_scan": ["path"],
    "std_check": ["path"],
    "cb_scan": ["path"],
    "locate_edit": ["path", "query"],
    "game_check": ["path"],
    "ide_references": ["root", "symbol"],
    "cb_status": ["path"],
}
SPECULATE_MAX = int(os.environ.get("UNIFIED_RX_SPECULATE_MAX", "3"))

_lock = threading.Lock()
_stats = {"predicted": 0, "executed": 0, "hit": 0, "errors": 0}


def _predict_next(current_file: str, recent_tools: list[str],
                  recent_paths: list[str]) -> list[dict]:
    """基于编辑上下文 + 历史调用模式预测下一步（启发式 + stats 数据驱动）。"""
    preds: list[dict] = []
    path = recent_paths[0] if recent_paths else os.path.dirname(current_file)
    # stats 数据驱动（2026-08-15 继续处理）：调用序列转移概率——
    # 最近一次调用的工具 → 下一步最可能（二元组转移计数）
    stats_preds = _predict_from_stats(recent_tools)
    for tool in stats_preds:
        if len(preds) >= SPECULATE_MAX:
            break
        if tool in SPECULATE_WHITELIST and tool not in [p["tool"] for p in preds]:
            preds.append({"tool": tool,
                          "args": {"path": path}})
    # 启发式补充（stats 未覆盖时）
    if len(preds) < SPECULATE_MAX and path:
        preds.append({"tool": "bug_scan", "args": {"path": path}})
    if len(preds) < SPECULATE_MAX and path:
        preds.append({"tool": "std_check", "args": {"path": path}})
    if len(preds) < SPECULATE_MAX and current_file:
        name = os.path.splitext(os.path.basename(current_file))[0]
        preds.append({"tool": "locate_edit", "args": {"path": path,
                                                      "query": name}})
    return preds[:SPECULATE_MAX]


def _predict_from_stats(recent_tools: list[str], limit: int = 3000) -> list[str]:
    """调用序列转移概率：读 stats.json 最近 limit 条 → 二元组 (A→B) 计数 →
    最近工具后最可能的下一步（按频次降序）。数据驱动——替代纯启发式。"""
    import collections
    try:
        stats_path = os.path.join(
            os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".",
            ".unified-rx", "stats.json")
        if not os.path.exists(stats_path):
            return []
        with open(stats_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        seq = [str(e.get("tool", "")) for e in data[-limit:]
               if isinstance(e, dict) and e.get("tool")]
        if len(seq) < 2:
            return []
        # 最近一次工具（优先用调用方传入的 recent_tools——更贴近当前会话）
        last = recent_tools[-1] if recent_tools else seq[-1]
        trans: dict[str, int] = collections.Counter()
        for i in range(len(seq) - 1):
            if seq[i] == last:
                trans[seq[i + 1]] += 1
        if not trans:
            # 回退：全局最频繁工具
            return [c[0] for c in collections.Counter(seq).most_common(3)]
        return [t for t, _ in trans.most_common(3)]
    except Exception:  # 尽力而为（stats 不可读时回退启发式）
        return []


def _key(tool: str, args: dict) -> str:
    sig = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return f"spec:{tool}:{hashlib.sha256(sig.encode('utf-8')).hexdigest()[:16]}"


def speculate(current_file: str = "", recent_tools: list[str] | None = None,
              recent_paths: list[str] | None = None) -> dict:
    """推测执行入口：预测 → 预执行（白名单只读）→ 结果进 ide_cache。"""
    global _stats
    recent_tools = recent_tools or []
    recent_paths = recent_paths or []
    preds = _predict_next(current_file, recent_tools, recent_paths)
    results: list[dict] = []
    for p in preds:
        tool = p["tool"]
        args = p.get("args") or {}
        if tool not in SPECULATE_WHITELIST:
            continue  # 白名单外绝不预执行
        k = _key(tool, args)
        with _lock:
            _stats["predicted"] += 1
        try:
            # 已有缓存 → 命中（不重复执行）
            cached = _cache_get(k)
            if cached is not None:
                with _lock:
                    _stats["hit"] += 1
                results.append({"tool": tool, "args": args,
                                "status": "cached", "preview": cached[:120]})
                continue
            # 预执行（幂等只读）
            from server import _call
            txt = _call(tool, args)[0].text
            _cache_put(k, txt)
            with _lock:
                _stats["executed"] += 1
            results.append({"tool": tool, "args": args,
                            "status": "executed", "preview": txt[:120]})
        except Exception as e:  # 尽力而为（预执行失败不影响主流程）
            with _lock:
                _stats["errors"] += 1
            results.append({"tool": tool, "args": args,
                            "status": "error", "error": str(e)[:80]})
    return {"ok": True, "predicted": preds, "results": results,
            "stats": dict(_stats),
            "note": "推测执行仅幂等只读白名单——实际调用命中缓存秒回"}


# ── 缓存（复用 ide_cache 语义——独立小缓存防污染主缓存）──
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 60.0  # 秒（推测结果短期有效）
_CACHE_MAX = 200


def _cache_get(k: str) -> str | None:
    with _lock:
        item = _CACHE.get(k)
        if item is None:
            return None
        ts, val = item
        if time.time() - ts > _CACHE_TTL:
            _CACHE.pop(k, None)
            return None
        return val


def _cache_put(k: str, val: str) -> None:
    with _lock:
        _CACHE[k] = (time.time(), val)
        if len(_CACHE) > _CACHE_MAX:
            # 简单淘汰最旧
            oldest = min(_CACHE, key=lambda kk: _CACHE[kk][0])
            _CACHE.pop(oldest, None)


def consume_speculated(tool: str, args: dict) -> str | None:
    """实际调用时消费缓存（命中 → 秒回；未命中 → None 走正常执行）。"""
    k = _key(tool, args)
    return _cache_get(k)
