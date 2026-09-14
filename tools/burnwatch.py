# -*- coding: utf-8 -*-
"""tools/burnwatch.py —— S141 会话烧量哨兵：model-io 体积分级告警。

动机（9/14 复盘）：真正烧钱的是 LLM 会话每轮重发的上下文——当天三个马拉松会话
合计 ~100MB 请求（300+ 轮、约 2500 万 token 量级），事后看账单才发现。本模块在
工具调用热路径上（节流 1 次/60s）看一眼 ZCode rollout 目录里会话文件的体积，
越过阈值就往 alarms.jsonl 写一条告警（每会话每档只报一次）——让"正在烧"在几分钟
内可见，而不是一天后看账单。

口径：体积≈累计 prompt 请求字节；混合中英 JSON 粗算 ~3.5 字节/token，
MB×~28 万 ≈ token 量级。只看目录元数据，不解析对话内容、不发任何外网请求。

旁路：`UNIFIED_RX_BURN_MB=0` 关闭；`UNIFIED_RX_ROLLOUT_DIR` 覆盖目录（测试用）。
探测自身绝不抛：哨兵坏掉不能拖垮工具调用。
"""
import glob
import os
import threading
import time

_LOCK = threading.Lock()
_LAST = 0.0          # 上次探测时刻（节流）
_SEEN = {}           # file -> {已告警档位}（有界滚动，防无界增长）
_INTERVAL = 60.0
_FRESH_S = 3600.0    # 只盯活跃会话：mtime 超过 1 小时的陈旧文件不告警（否则重启即刷旧账）


def _rollout_dir():
    d = os.environ.get("UNIFIED_RX_ROLLOUT_DIR") or ""
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout")


def _threshold_mb():
    try:
        return max(0, int(str(os.environ.get("UNIFIED_RX_BURN_MB", "") or 15)))
    except (TypeError, ValueError):
        return 15


def sessions():
    """rollout 目录里的会话文件（名字/体积/修改时间），按体积降序。只读目录元数据。"""
    out = []
    pattern = os.path.join(_rollout_dir(), "model-io-sess_*.jsonl")
    for p in glob.glob(pattern):
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"file": os.path.basename(p), "mb": round(st.st_size / 1e6, 1),
                    "mtime": int(st.st_mtime)})
    out.sort(key=lambda r: -r["mb"])
    return out


def _alarm(rule, msg, level="WARN"):
    try:
        from tools import breaker as _b
        _b._alarm(rule, msg, level)
    except Exception:                                              # noqa: BLE001
        pass


def _sid(name):
    parts = str(name).split("_", 1)
    return parts[1][:8] if len(parts) > 1 else name[:12]


def maybe_check():
    """热路径调用（registry._record_stats）：节流 60s，越档告警一次/档。绝不抛。"""
    global _LAST
    thr = _threshold_mb()
    if thr <= 0:
        return
    now = time.time()
    with _LOCK:
        if now - _LAST < _INTERVAL:
            return
        _LAST = now
    try:
        for s in sessions():
            if now - s.get("mtime", 0) > _FRESH_S:
                continue                        # 陈旧会话不报警（已结束的账不再刷）
            tier = int(s["mb"] // thr)          # 15MB→第1档、30MB→第2档……
            if tier < 1:
                break
            with _LOCK:
                seen = _SEEN.setdefault(s["file"], set())
                if tier in seen:
                    continue
                seen.add(tier)
                if len(_SEEN) > 64:             # 老会话滚动清理
                    for k in list(_SEEN)[:-32]:
                        _SEEN.pop(k, None)
            est = int(s["mb"] * 280_000)
            _alarm("session_burn",
                   f"会话 {_sid(s['file'])} 的 model-io 已达 {s['mb']}MB"
                   f"（≈{est // 10000}万 token 量级累计请求，第 {tier} 档/阈值 {thr}MB）"
                   f"——马拉松会话每轮重发全上下文，建议收尾或新开会话")
    except Exception:                                              # noqa: BLE001
        pass
