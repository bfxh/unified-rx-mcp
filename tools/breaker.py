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

S140 全局护栏（与 per-key 熔断互补）：per-key 只拦"重复"，拦不住"变着参数穷举"
和"脚本级洪峰"（脚本/测试直调 registry.call 每次参数都不同）。因此加两道总量闸：
- 全局 QPM：60s 滑动窗口内**全部工具**合计调用超过 `UNIFIED_RX_GLOBAL_QPM`
  （默认 3000，0=off）次 → 全局熔断，冷却 `UNIFIED_RX_GLOBAL_COOLDOWN_S`
  （默认 60s）内除豁免工具（breaker_status/reset）外一律拒绝；
- 每日总量告警：本地日历日调用数达到 `UNIFIED_RX_DAILY_ALERT`（默认 100000，
  0=off）→ 写一条 alarms.jsonl 告警（只告警不阻断，跨天自动归零，reset 不清——
  防止救火复位后被再次拉爆）。

S141 标定与持久化（9/14 复盘）：
- QPM 默认从 600 上调到 3000——实测正常重度工作日峰值 ~1600 次/分钟（并行批量
  扫描），600 会误伤正常工作；而失控洪峰是 ~10000 次/分钟量级，3000 仍能拦。
- 日计数落盘 `~/.unified-rx/daily_state.json`（节流写，原子替换）：server 一天
  重启多次，纯内存计数会被反复清零、日告警形同虚设。

诚实边界：这是**循环刹车，不是安全边界**——只拦"重复"与"总量洪峰"，拦不住
"低于阈值的持续慢漏"；参数规范化是 JSON 排序序列化，语义等价但写法不同的参数可能
漏判。熔断器自身绝不抛穿（registry 侧 try/except 兜底）：刹车坏掉不能拖垮工具。
"""
import hashlib
import json
import os
import threading
import time

from registry import tool

_LOCK = threading.Lock()
_HISTORY: dict[str, list[float]] = {}      # key -> [ts...]（窗口内调用时间戳）
_META: dict[str, dict] = {}         # key -> {"tool": name, "args": 摘要}（状态可读）
_STREAK: dict[str, list] = {}       # key -> [result_hash, 连续相同次数]
_TRIPPED: dict[str, tuple] = {}      # key -> (until_ts, reason)
_MAX_KEYS = 4096   # 内存上限：超过就清最老的记录（熔断器不能自己变成内存泄漏）

# S140 全局护栏状态（不分工具、不分参数——专收 per-key 拦不住的洪峰）
_GLOBAL_HIST: list[float] = []      # 60s 窗口内全部调用时间戳
_GLOBAL_UNTIL = 0.0    # 全局熔断截止时刻（0=未熔断）
_GLOBAL_REASON = ""
_DAILY = {"day": None, "count": 0, "alerted": False}   # 当日总量（reset 不清，跨天归零）

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


def _env_int0(name, default):
    """同 _env_int 但允许 0（S140 的全局护栏 0=off）。"""
    try:
        return max(0, int(str(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _global_qpm():
    return _env_int0("UNIFIED_RX_GLOBAL_QPM", 3000)


def _global_cooldown():
    return _env_int0("UNIFIED_RX_GLOBAL_COOLDOWN_S", 60)


def _daily_alert_at():
    return _env_int0("UNIFIED_RX_DAILY_ALERT", 100000)


def _alarm_file():
    return os.path.join(os.path.expanduser("~"), ".unified-rx", "alarms.jsonl")


def _alarm(rule, msg, level="WARN"):
    """S140：总量越限告警（alarms.jsonl，与既有告警同格式）。绝不抛。"""
    try:
        rec = {"rule": rule, "target": "*", "level": level, "msg": msg, "ts": time.time()}
        with open(_alarm_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _daily_path():
    """S141 日计数落点：~/.unified-rx/daily_state.jsonl（追加式，与 alarms/stats 同 idiom）。
    目录=固定常量段；文件名=常量字面量，整条路径无外部输入。"""
    home_dir = os.path.join(os.path.expanduser("~"), ".unified-rx")
    return os.path.join(home_dir, "daily_state.jsonl")


def _daily_load():
    """S141：读末行有效记录恢复当日计数；隔日旧档归零；坏档当空账。绝不抛。"""
    try:
        last = None
        with open(_daily_path(), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    last = ln
        if not last:
            return {"day": None, "count": 0, "alerted": False}
        d = json.loads(last)
        day = str(d.get("day") or "")
        if day == time.strftime("%Y-%m-%d"):
            return {"day": day, "count": max(0, int(d.get("count") or 0)),
                    "alerted": bool(d.get("alerted"))}
        return {"day": day or None, "count": 0, "alerted": False}
    except (OSError, ValueError, TypeError):
        return {"day": None, "count": 0, "alerted": False}


def _daily_save():
    """S141：日计数追加落盘（节流由调用方控制；失败绝不抛）。须持 _LOCK 调用。"""
    try:
        rec = {"day": _DAILY["day"], "count": _DAILY["count"],
               "alerted": _DAILY["alerted"], "ts": int(_now())}
        with open(_daily_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


_DAILY.update(_daily_load())   # S141：跨 server 重启续账（一日多启，内存计数会被清零）


def _daily_bump(now_ts):
    """S140/S141：当日总量 +1；达标写一次告警（当天不重复）；节流持久化。
    须持 _LOCK 调用。"""
    day = time.strftime("%Y-%m-%d", time.localtime(now_ts))
    if _DAILY["day"] != day:
        _DAILY["day"] = day
        _DAILY["count"] = 0
        _DAILY["alerted"] = False
    _DAILY["count"] += 1
    at = _daily_alert_at()
    if at > 0 and not _DAILY["alerted"] and _DAILY["count"] >= at:
        _DAILY["alerted"] = True
        _alarm("daily_calls", f"当日工具调用 {_DAILY['count']} 次达到告警阈值 {at}")
        _daily_save()
        return
    if _DAILY["count"] == 1 or _DAILY["count"] % 100 == 0:
        _daily_save()


def _global_trip_msg(left_s):
    return (f"BreakerOpen: 全局熔断——所有工具暂停 {left_s}s（全局 QPM 超限，"
            f"疑似脚本级洪峰/变参穷举循环）。等冷却自动恢复，或 breaker_reset 复位；"
            f"阈值 UNIFIED_RX_GLOBAL_QPM={_global_qpm()}/60s / 冷却 {_global_cooldown()}s")


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
    global _GLOBAL_UNTIL, _GLOBAL_REASON
    if not enabled() or tool_name in _EXEMPT:
        return None
    key = _key(tool_name, args, cursor)
    now = _now()
    with _LOCK:
        # S140 全局闸：60s 内全部工具合计超 QPM → 全局熔断（洪峰期间持续续闸，
        # 洪峰停止后窗口自然滑空 → 自动恢复）。全局熔断期间不再累加日计数——
        # 被拒的调用不是消耗。
        gq = _global_qpm()
        if gq > 0:
            gcut = now - 60.0
            while _GLOBAL_HIST and _GLOBAL_HIST[0] < gcut:
                _GLOBAL_HIST.pop(0)
            _GLOBAL_HIST.append(now)
            if now < _GLOBAL_UNTIL:
                return _global_trip_msg(int(_GLOBAL_UNTIL - now) + 1)
            if len(_GLOBAL_HIST) > gq:
                _GLOBAL_UNTIL = now + _global_cooldown()
                _GLOBAL_REASON = f"全部工具 60s 内合计调用 {len(_GLOBAL_HIST)} 次（>{gq}）"
                _GLOBAL_HIST.clear()
                _alarm("global_qpm", f"{_GLOBAL_REASON}，全局熔断 {_global_cooldown()}s")
                return _global_trip_msg(_global_cooldown())
        _daily_bump(now)
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
    """复位：给 tool_name 只清该工具；不给则全清。返回 (清掉的 key 数, 剩余 key 数)。

    S140：per-key 计数与全局熔断态都清；**日总量计数保留**（跨天自动归零）——
    它是安全计量，清零会让"复位→再拉爆→再复位"绕过每日告警。"""
    global _GLOBAL_UNTIL, _GLOBAL_REASON
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
        _GLOBAL_HIST.clear()
        _GLOBAL_UNTIL = 0.0
        _GLOBAL_REASON = ""
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
        gq = _global_qpm()
        g_tripped = now < _GLOBAL_UNTIL
        return {"enabled": enabled(), "limit": _limit(), "window_s": _window(),
                "cooldown_s": _cooldown(), "tracked_keys": len(_META),
                "tripped": sorted(tripped, key=lambda x: -x["left_s"]),
                "busiest": [t for t in top if t["calls_in_window"] > 1],
                "global": {"qpm": gq,
                           "calls_last_60s": len(_GLOBAL_HIST),
                           "tripped": g_tripped,
                           "tripped_left_s": int(_GLOBAL_UNTIL - now) + 1 if g_tripped else 0,
                           "reason": _GLOBAL_REASON if g_tripped else "",
                           "daily_calls": _DAILY["count"],
                           "daily_alert_at": _daily_alert_at(),
                           "daily_alerted": _DAILY["alerted"]}}


@tool("breaker_status", "工具熔断状态：per-key 重复计数 / 全局 QPM 与日总量护栏 / 阈值与旁路开关"
      "（同一工具+参数窗口内 >limit 次即断；全部工具 60s 合计 >QPM 即全局断）", "guard",
      {"type": "object", "properties": {}, "required": []})
def breaker_status():
    st = snapshot()
    st["note"] = ("同一（工具+参数）在窗口内调用超过 limit 次 → 熔断并冷却；"
                  "结果连续逐字节相同同样触发（空转）；全部工具 60s 合计超过 "
                  "UNIFIED_RX_GLOBAL_QPM → 全局熔断（拦变参穷举/脚本洪峰）；"
                  "当日调用达 UNIFIED_RX_DAILY_ALERT → alarms.jsonl 告警（不阻断）。"
                  "改参数/换目标即刻恢复，breaker_reset 可复位；"
                  "UNIFIED_RX_BREAKER=off 旁路。这是循环刹车，不是安全边界")
    return st


@tool("breaker_reset", "复位工具熔断：清空计数与熔断态（给 tool 只复位该工具；日总量计量保留）", "guard",
      {"type": "object",
       "properties": {"tool": {"type": "string", "description": "只复位该工具（缺省=全清）"}},
       "required": []})
def breaker_reset(tool=None):
    cleared, left = reset(tool)
    return {"cleared_keys": cleared, "remaining_keys": left,
            "note": "已复位；重复调用计数从零开始"}
