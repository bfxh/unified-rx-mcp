#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session-guard 钩子（ZCode，zcode-breaker 插件）—— 会话烧量哨兵（S141）。

用法（hooks/hooks.json 传入）：
    session_guard_hook.py start    # SessionStart：注入"会话卫生"规则 + 今日已烧量
    session_guard_hook.py prompt   # UserPromptSubmit：本会话越档 → 注入收尾建议
    （PostToolUse 的检查由 breaker_hook.py 内部 import tick() 复用，不单起进程）

口径：ZCode 把每轮模型请求写到 ~/.zcode/cli/rollout/model-io-<session_id>.jsonl，
文件体积≈累计 prompt 字节（MB×~28万≈token 量级，混合中英粗估）。会话越长每轮重发
越多（成本二次增长），因此档位（15/30/50/80MB）每升一档提醒一次：落盘结论、收尾、
新开会话。为何是"注入对话"而不是只写文件：烧钱发生在会话内，提醒必须让智能体看见。

纪律（与 breaker_hook.py 同款）：
- 读不懂输入一律放行（exit 0）；本钩子从不阻断（永不 exit 2）；
- 会话 ID **不进路径**——只在 rollout 目录的**列举结果**里做相等比较来定位文件，
  命中后再校验解析结果仍在目录内（越界即放弃）；
- 状态固定落系统临时目录 zcode-session-guard-state.jsonl，**追加写、末行生效**，
  仅在档位上升时追加（每会话至多 4 行），不创建目录、不截断写。
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time

TIERS_MB = (15, 30, 50, 80)
_FRESH_S = 900.0        # 找不到本会话文件时，回退到最近 15 分钟内活跃的会话文件
_PREFIX = "model-io-"
_SUFFIX = ".jsonl"


def _rollout_dir():
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout")


def _state_path():
    return os.path.join(tempfile.gettempdir(), "zcode-session-guard-state.jsonl")


def _load_state():
    """末行生效：返回 {文件basename哈希: 已告警档位}。"""
    last = None
    try:
        with open(_state_path(), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    last = ln
    except OSError:
        return {}
    try:
        st = json.loads(last) if last else {}
        return st if isinstance(st, dict) else {}
    except ValueError:
        return {}


def _append_state(st):
    """追加一行（末行生效语义）。失败绝不抛。"""
    try:
        with open(_state_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(st, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _iter_sess_files():
    """列举 rollout 目录里的会话文件（仅目录元数据）。返回 [(path, mb, mtime)]。"""
    base = _rollout_dir()
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        if not (name.startswith(_PREFIX) and name.endswith(_SUFFIX)):
            continue
        p = os.path.join(base, name)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        out.append((p, stt.st_size / 1e6, stt.st_mtime))
    return out


def _find_sess(session_id):
    """按会话 ID 在**列举结果**里比对定位文件；ID 不进路径拼接。"""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:80]
    if not sid:
        return None
    base = os.path.abspath(_rollout_dir())
    for p, mb, mt in _iter_sess_files():
        name = os.path.basename(p)
        if name[len(_PREFIX):-len(_SUFFIX)] != sid:
            continue
        if os.path.dirname(os.path.abspath(p)) != base:
            continue                                        # 双保险：结果必须仍在目录内
        return (p, mb, mt)
    return None


def _newest_fresh():
    best = None
    now = time.time()
    for p, mb, mt in _iter_sess_files():
        if now - mt > _FRESH_S:
            continue
        if best is None or mt > best[2]:
            best = (p, mb, mt)
    return best


def _today_mb():
    """今日（本地日）rollout 体积：mtime 落在今天的会话文件之和。"""
    day = time.strftime("%Y-%m-%d")
    total = 0.0
    for p, mb, mt in _iter_sess_files():
        if time.strftime("%Y-%m-%d", time.localtime(mt)) == day:
            total += mb
    return total


def _tier(mb):
    n = 0
    for i, t in enumerate(TIERS_MB):
        if mb >= t:
            n = i + 1
    return n


def _wan(mb):
    return int(mb * 28)          # MB → 万 token 量级（280000/10000）


def burn_check(session_id=None):
    """越档返回提醒文案（并记账），否则 None。绝不抛。"""
    try:
        hit = _find_sess(session_id) or _newest_fresh()
        if not hit:
            return None
        p, mb, mt = hit
        tier = _tier(mb)
        if tier < 1:
            return None
        st = _load_state()
        key = hashlib.sha256(os.path.basename(p).encode("utf-8", "replace")).hexdigest()[:16]
        if tier <= int(st.get(key) or 0):
            return None
        st[key] = tier
        _append_state(st)
        return (f"[session-guard] 烧量哨兵：本会话 model-io 已 {mb:.1f}MB"
                f"（≈{_wan(mb)}万 token 量级累计请求，越第 {tier} 档 {TIERS_MB[tier - 1]}MB）。"
                f"每轮都要重发全部上下文，成本随会话长度二次增长——建议把结论/进度落盘后"
                f"新开会话继续；全量视图用 session_burn 工具。")
    except Exception:                                          # noqa: BLE001
        return None


def tick(session_id=None):
    """PostToolUse 入口（breaker_hook 调用）：仅在档位上升时给文案。绝不抛。"""
    return burn_check(session_id)


def _emit(event_name, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}, ensure_ascii=False))


def _start_msg():
    total = _today_mb()
    parts = ["[session-guard] 会话卫生：长任务拆会话——每项目/每阶段新开或 /clear；"
             "单会话 model-io 超 15MB 就该收尾（15/30/50/80MB 各提醒一次；"
             "resume/compact 恢复大会话前先看体积）"]
    if total > 0:
        parts.append(f"今日已烧 ≈{total:.0f}MB（≈{_wan(total)}万 token 量级累计请求）")
    parts.append("实时查询：session_burn 工具；告警历史 ~/.unified-rx/alarms.jsonl")
    return "。".join(parts) + "。"


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if os.environ.get("ZCODE_SESSION_GUARD", "on").strip().lower() in ("off", "0", "false", "no"):
        return 0
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        if not isinstance(ev, dict):
            return 0
    except Exception:                                          # noqa: BLE001
        return 0                       # 读不懂 → 放行
    sid = ev.get("session_id")
    if mode == "start":
        _emit("SessionStart", _start_msg())
        return 0
    if mode == "prompt":
        msg = burn_check(sid)
        if msg:
            _emit("UserPromptSubmit", msg)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
