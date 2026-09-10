# -*- coding: utf-8 -*-
"""tools/breaker.py —— 工具熔断（S122）：同一（工具+参数）重复超限即断。

动机：死循环里 agent 会反复执行同一条命令（同工具同参数），靠模型自觉停不下来。
本模块挂在 `registry.call` 这个单一裁决点上做滑动窗口计数，超限即拒绝执行并给出
可操作提示——**与 ZCode 插件 `plugins/zcode-breaker` 同规则**（宿主级 + 工具级双保险）。

口径：
- 窗口 `UNIFIED_RX_BREAKER_WINDOW_S`（默认 300s）内，同一 key（工具 + 规范化参数 +
  cursor）调用 **超过** `UNIFIED_RX_BREAKER_LIMIT`（默认 10）次 → 熔断；冷却
  `UNIFIED_RX_BREAKER_COOLDOWN_S`（默认 120s）内该 key 一律拒绝；
- 冷却到期自动恢复（**改参数/换目标立即恢复**，因为 key 变了）；也可显式
  `breaker_reset` 复位；
- 结果空转检测：同一 key 连续返回**逐字节相同**的结果达阈值 → 提前熔断（同一命令
  反复空转的强信号，提示语不同）；
- 旁路：`UNIFIED_RX_BREAKER=off`（宿主/测试用）；`breaker_status` 看状态。

诚实边界：这是**循环刹车，不是安全边界**——只拦"重复"，拦不住"每次换参数的穷举"；
参数规范化是 JSON 排序序列化，语义等价但写法不同的参数可能漏判。熔断器自身绝不
抛穿（registry 侧 try/except 兜底）：刹车坏掉不能拖垮工具。
"""
import hashlib
import json
import os
import threading
import time

from registry import tool

_LOCK = threading.Lock()
_HISTORY = {}      # key -> [ts...]（窗口内调用时间戳）
_META = {}         # key -> {"tool": name, "args": 摘要}（状态可读）
_STREAK = {}       # key -> [result_hash, 连续相同次数]
_TRIPPED = {}      # key -> (until_ts, reason)
_MAX_KEYS = 4096   # 内存上限：超过就清最老的记录（熔断器不能自己变成内存泄漏）

# 熔断器的自检/复位工具本身豁免——否则救火的人被火拦住
_EXEMPT = frozenset({"breaker_status", "breaker_reset"})


def _now():
    """时钟入口（测试可 monkeypatch，避免冻结全局 time）。"""
    return time.time()


def _env_int(name, default):
    try:
        return max(1, int(str(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _limit():
    return _env_int("UNIFIED_RX_BREAKER_LIMIT", 10)


def _window():
    return _env_int("UNIFIED_RX_BREAKER_WINDOW_S", 300)


def _cooldown():
    return _env_int("UNIFIED_RX_BREAKER_COOLDOWN_S", 120)


def enabled():
    return os.environ.get("UNIFIED_RX_BREAKER", "on").strip().lower() not in ("off", "0", "false", "no")


def _key(tool_name, args, cursor=None):
    try:
        payload = json.dumps({"tool": tool_name, "args": args, "cursor": cursor},
                             ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = f"{tool_name}:{args!r}:{cursor!r}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _args_digest(args):
    try:
        s = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = repr(args)
    return s[:160]


def _result_hash(result):
    try:
        s = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = repr(result)
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


def _evict_if_needed():
    if len(_META) <= _MAX_KEYS:
        return
    for k in list(_META)[: len(_META) - _MAX_KEYS]:
        _META.pop(k, None)
        _HISTORY.pop(k, None)
        _STREAK.pop(k, None)
        _TRIPPED.pop(k, None)


def _trip_msg(key, tool_name, reason):
    return (f"BreakerOpen: 熔断——{reason}（工具 {tool_name}，key {key}）。"
            f"改参数/换目标即刻恢复，或调 breaker_reset 复位；"
            f"阈值 UNIFIED_RX_BREAKER_LIMIT={_limit()} / 窗口 {_window()}s / 冷却 {_cooldown()}s")


def check(tool_name, args, cursor=None):
    """调用前门：放行返回 None；熔断返回拒绝原因字符串（registry 转 ok:false）。"""
    if not enabled() or tool_name in _EXEMPT:
        return None
    key = _key(tool_name, args, cursor)
    now = _now()
    with _LOCK:
        hit = _TRIPPED.get(key)
        if hit:
            until, reason = hit
            if now < until:
                left = int(until - now) + 1
                return _trip_msg(key, tool_name, f"{reason}，冷却剩余 {left}s")
            _TRIPPED.pop(key, None)
            _HISTORY.pop(key, None)
            _STREAK.pop(key, None)
        hist = _HISTORY.setdefault(key, [])
        cut = now - _window()
        while hist and hist[0] < cut:
            hist.pop(0)
        hist.append(now)
        _META[key] = {"tool": tool_name, "args": _args_digest(args)}
        if len(hist) > _limit():
            _TRIPPED[key] = (now + _cooldown(), f"同一工具+参数 {_window()}s 内调用 {len(hist)} 次")
            _HISTORY.pop(key, None)
            _STREAK.pop(key, None)
            _evict_if_needed()
            return _trip_msg(key, tool_name, f"同一工具+参数 {_window()}s 内调用 {len(hist)} 次（>{_limit()}）")
    return None


def record(tool_name, args, result, cursor=None):
    """调用后记录：同一 key 连续返回逐字节相同结果达阈值 → 提前熔断（空转信号）。"""
    if not enabled() or tool_name in _EXEMPT:
        return
    key = _key(tool_name, args, cursor)
    rh = _result_hash(result)
    now = _now()
    with _LOCK:
        if key in _TRIPPED:
            return
        prev = _STREAK.get(key)
        if prev and prev[0] == rh:
            prev[1] += 1
            if prev[1] >= _limit():
                _TRIPPED[key] = (now + _cooldown(),
                                 f"同一工具+参数连续 {prev[1]} 次返回完全相同的结果（空转）")
                _HISTORY.pop(key, None)
                _STREAK.pop(key, None)
                _evict_if_needed()
        else:
            _STREAK[key] = [rh, 1]


def reset(tool_name=None):
    """复位：给 tool_name 只清该工具；不给则全清。返回 (清掉的 key 数, 剩余 key 数)。"""
    with _LOCK:
        if tool_name:
            keys = [k for k, m in _META.items() if m.get("tool") == tool_name]
        else:
            keys = list(set(_META) | set(_TRIPPED) | set(_HISTORY) | set(_STREAK))
        for k in keys:
            _META.pop(k, None)
            _HISTORY.pop(k, None)
            _STREAK.pop(k, None)
            _TRIPPED.pop(k, None)
        return len(keys), len(_META)


def snapshot():
    """状态快照（breaker_status 用）：不含参数原文，只给摘要。"""
    now = _now()
    with _LOCK:
        tripped = []
        for k, (until, reason) in _TRIPPED.items():
            if until <= now:
                continue
            tripped.append({"key": k, "tool": _META.get(k, {}).get("tool", "?"),
                            "args": _META.get(k, {}).get("args", ""),
                            "left_s": int(until - now) + 1, "reason": reason})
        top = sorted(({"key": k, "tool": m.get("tool", "?"), "args": m.get("args", ""),
                       "calls_in_window": len(_HISTORY.get(k, []))}
                      for k, m in _META.items()),
                     key=lambda x: -x["calls_in_window"])[:10]
        return {"enabled": enabled(), "limit": _limit(), "window_s": _window(),
                "cooldown_s": _cooldown(), "tracked_keys": len(_META),
                "tripped": sorted(tripped, key=lambda x: -x["left_s"]),
                "busiest": [t for t in top if t["calls_in_window"] > 1]}


@tool("breaker_status", "工具熔断状态：窗口内重复调用计数 / 已熔断的 key / 阈值与旁路开关"
      "（同一工具+参数窗口内 >limit 次即断）", "meta",
      {"type": "object", "properties": {}, "required": []})
def breaker_status():
    st = snapshot()
    st["note"] = ("同一（工具+参数）在窗口内调用超过 limit 次 → 熔断并冷却；"
                  "结果连续逐字节相同同样触发（空转）。改参数/换目标即刻恢复，"
                  "breaker_reset 可复位；UNIFIED_RX_BREAKER=off 旁路。"
                  "这是循环刹车，不是安全边界")
    return st


@tool("breaker_reset", "复位工具熔断：清空计数与熔断态（给 tool 只复位该工具）", "meta",
      {"type": "object",
       "properties": {"tool": {"type": "string", "description": "只复位该工具（缺省=全清）"}},
       "required": []})
def breaker_reset(tool=None):
    cleared, left = reset(tool)
    return {"cleared_keys": cleared, "remaining_keys": left,
            "note": "已复位；重复调用计数从零开始"}
