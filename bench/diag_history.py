# -*- coding: utf-8 -*-
"""S50：诊断历史持久化——每轮 verify/repair 的诊断摘要追加 JSONL。

格式：{"ts", "iid", "arm", "phase", "findings_total", "by_severity",
       "diags": [{source,file,line,severity,message}]}
跨会话可比对：这轮修好了几个 / 哪些是新引入的。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results", "diag_history.jsonl")


def append_diag(iid, arm, phase, diags):
    """diags = 统一形状 [{source,file,line,severity,message}]。幂等追加。"""
    if not diags:
        return
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "iid": iid, "arm": arm,
             "phase": phase,
             "total": len(diags),
             "diags": diags[:100]}
    with open(OUT, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def diff_since(since_ts, iid_prefix=""):
    """读历史，返回 since_ts 之后的条目（跨会话比对用）。"""
    out = []
    if not os.path.exists(OUT):
        return out
    for line in open(OUT, encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ts", "") >= since_ts and e.get("iid", "").startswith(iid_prefix):
            out.append(e)
    return out
