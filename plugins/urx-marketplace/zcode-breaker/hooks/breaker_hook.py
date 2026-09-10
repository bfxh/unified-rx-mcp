#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zcode-breaker 钩子（ZCode）—— 工具熔断，与 unified-rx-mcp tools/breaker.py 同规则。

用法（由 hooks/hooks.json 传入）：
    breaker_hook.py <pre|post|postfail|prompt> <limit> <window_s> <cooldown_s>

规则：
- `pre`：同一（工具 + 规范化参数）在窗口内调用**超过** limit 次 → 退出码 2 阻断
  （ZCode 把 stderr 作为阻断原因展示）；冷却期内同 key 一律阻断。
- `post`/`postfail`：同一 key 连续返回**逐字节相同**的结果达 limit 次 → 标记熔断
  （下一次 pre 阻断）——同一命令反复空转的强信号。
- `prompt`：同一指令文本重复超 limit 次 → 只**告警**（additionalContext 注入），
  不阻断用户输入（用户说"继续"是正常用法，阻断会把人锁在外面）。
- 旁路：环境变量 `ZCODE_BREAKER=off`；状态文件按会话隔离，删除即复位。

纪律：钩子读不懂输入一律**放行**（exit 0）——刹车坏了不能把宿主搞停；
阈值/窗口/冷却在 hooks.json 的 args 里调，改完无需重启（每次调用重新读）。

路径纪律：状态文件固定落在系统临时目录的 `zcode-breaker/state.json`（pathlib
字面量组件构造，不经参数流入）；会话 ID 只做 SHA-256 当数据键，不进路径。
"""
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time

_MAX_SESSIONS = 32


def _state_path():
    return pathlib.Path(tempfile.gettempdir(), "zcode-breaker", "state.json")


def _load():
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st):
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
    except OSError:
        pass


def _session_state(st, session):
    """取（或建）本会话的状态块：会话 ID 只做哈希当键，不进任何路径。"""
    sid = hashlib.sha256((session or "default").encode("utf-8", "replace")).hexdigest()[:16]
    sessions = st.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    s = sessions.get(sid)
    if not isinstance(s, dict):
        s = {}
    sessions[sid] = s
    while len(sessions) > _MAX_SESSIONS:          # 只留最近的，防无限增长
        sessions.pop(next(iter(sessions)))
    st["sessions"] = sessions
    return s


def _digest(payload):
    try:
        s = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = repr(payload)
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


def _trim(hist, now, window):
    return [t for t in hist if isinstance(t, (int, float)) and t > now - window]


def main(argv):
    mode = argv[1] if len(argv) > 1 else "pre"
    limit = int(argv[2]) if len(argv) > 2 else 10
    window = int(argv[3]) if len(argv) > 3 else 300
    cooldown = int(argv[4]) if len(argv) > 4 else 120
    if os.environ.get("ZCODE_BREAKER", "on").strip().lower() in ("off", "0", "false", "no"):
        return 0
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        if not isinstance(ev, dict):
            return 0
    except Exception:
        return 0                       # 读不懂 → 放行

    st = _load()
    s = _session_state(st, ev.get("session_id"))
    now = time.time()

    if mode == "pre":
        tool = str(ev.get("tool_name") or "")
        key = _digest({"t": tool, "i": ev.get("tool_input")})
        trips = s.get("x") if isinstance(s.get("x"), dict) else {}
        until = trips.get(key)
        if isinstance(until, (int, float)) and until > now:
            sys.stderr.write(
                f"熔断：工具 {tool} 同一参数已重复超限，冷却剩余 {int(until - now) + 1}s。"
                f"改参数/换目标即刻恢复；或删除状态文件 {_state_path()}\n")
            return 2
        hist = s.get("h") if isinstance(s.get("h"), dict) else {}
        cur = _trim(hist.get(key) or [], now, window)
        cur.append(now)
        if len(cur) > limit:
            trips[key] = now + cooldown
            hist.pop(key, None)
            s["h"], s["x"] = hist, trips
            _save(st)
            sys.stderr.write(
                f"熔断：工具 {tool} 同一参数 {window}s 内调用 {len(cur)} 次（>{limit}）。"
                f"冷却 {cooldown}s；改参数/换目标即刻恢复；或删除状态文件 {_state_path()}\n")
            return 2
        hist[key] = cur
        s["h"] = hist
        _save(st)
        return 0

    if mode in ("post", "postfail"):
        tool = str(ev.get("tool_name") or "")
        key = _digest({"t": tool, "i": ev.get("tool_input")})
        payload = ev.get("tool_response") if mode == "post" else ev.get("error_details")
        rh = _digest(payload)
        streaks = s.get("s") if isinstance(s.get("s"), dict) else {}
        prev = streaks.get(key)
        if isinstance(prev, list) and len(prev) == 2 and prev[0] == rh:
            prev[1] = int(prev[1]) + 1
        else:
            prev = [rh, 1]
        streaks[key] = prev
        s["s"] = streaks
        if prev[1] >= limit:
            trips = s.get("x") if isinstance(s.get("x"), dict) else {}
            trips[key] = now + cooldown
            s["x"] = trips
            streaks.pop(key, None)
        _save(st)
        return 0

    if mode == "prompt":
        text = str(ev.get("prompt") or "").strip()
        if not text:
            return 0
        key = _digest({"p": text})
        counts = s.get("p") if isinstance(s.get("p"), dict) else {}
        counts[key] = int(counts.get(key) or 0) + 1
        s["p"] = counts
        _save(st)
        if counts[key] > limit:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"[zcode-breaker] 同一指令已重复 {counts[key]} 次（阈值 {limit}）——"
                        f"可能陷入循环。建议改写指令/换目标；确需重复请删状态文件 "
                        f"{_state_path()}。"),
                }
            }, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
