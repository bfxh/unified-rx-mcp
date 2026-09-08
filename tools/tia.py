# -*- coding: utf-8 -*-
"""tools/tia.py —— 测试影响分析（S104）：状态与选择逻辑（纯函数 + 进程内状态）。

- 状态：root -> {"snapshot": {相对路径: 指纹}, "deps": {nodeid: [相对路径]}}，
  **进程内**保存（MCP 服务器常驻；跨重启不保留——与 tools/cache.py 同边界）。
- 选择口径（保守，宁多跑不误跳）：
    必跑 = ①依赖集与"变更文件集"相交；②无依赖记录（未知/上次没跑到）；
          ③本次新出现的测试（不在上次 deps 里）
    可跳 = 有依赖记录、且依赖集与变更集不相交
- 变更集 = 上次快照与本次快照的对称差（新增/删除/内容或元数据变化）。
- 任何输入不可用（快照 None / 依赖图缺失 / 收集失败）→ 调用方走全量。
"""
import threading

_LOCK = threading.Lock()
_STATE = {}          # root -> {"snapshot": {...}, "deps": {...}}


def get(root):
    with _LOCK:
        st = _STATE.get(root)
        return dict(st) if st else None


def put(root, snapshot, deps):
    with _LOCK:
        _STATE[root] = {"snapshot": dict(snapshot or {}), "deps": dict(deps or {})}


def reset():
    """清空状态（测试与排障用）。"""
    with _LOCK:
        _STATE.clear()


def changed_files(prev_snapshot, cur_snapshot):
    """对称差：值不同 / 只在一边出现的相对路径。"""
    prev = prev_snapshot or {}
    cur = cur_snapshot or {}
    out = set()
    for k, v in cur.items():
        if prev.get(k) != v:
            out.add(k)
    for k in prev:
        if k not in cur:
            out.add(k)
    return out


def select(collected, prev_deps, changed):
    """返回 (selected_nodeids, stats)。collected 为本次收集到的全部 nodeid。"""
    deps = prev_deps or {}
    chg = set(changed or ())
    selected, unknown, affected = [], [], []
    for nodeid in collected:
        d = deps.get(nodeid)
        if d is None:                 # 新测试/上次没跑到 → 必跑
            unknown.append(nodeid)
            selected.append(nodeid)
            continue
        if not d:                     # 有记录但依赖为空（无法判定）→ 必跑
            unknown.append(nodeid)
            selected.append(nodeid)
            continue
        if chg and (set(d) & chg):
            affected.append(nodeid)
            selected.append(nodeid)
    return selected, {
        "collected": len(collected),
        "selected": len(selected),
        "skipped": len(collected) - len(selected),
        "affected_by_change": len(affected),
        "unknown_or_new": len(unknown),
        "changed_files": sorted(chg)[:20],
        "changed_count": len(chg),
    }
