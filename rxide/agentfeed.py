# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/agentfeed.py — 只读查询端点逻辑：目录列举、文本搜索、git status、
telemetry.jsonl 增量 feed 与智能体状态（支撑前端 TRAE 式侧栏与智能体跟踪）。

纯 stdlib；大文件尾部采用 seek 块读（不全量读 7MB telemetry）。
telemetry 路径仿 termlog._log_path 的环境变量覆盖模式（UNIFIED_RX_TELEMETRY）。
"""
import json
import os
import re
import subprocess
import time

# telemetry.jsonl 默认路径（append-only，kind=tool / kind=hb 两类行）
_TELEMETRY_DEFAULT = os.path.join(os.path.expanduser("~"),
                                  ".unified-rx", "telemetry.jsonl")
_CHUNK = 65536          # 尾部块读步长
_TAIL_CAP = 200         # cursor=0 / 轮转重置时回放的最近行数
_STATUS_TAIL = 50       # agent/status 读尾行数
_ACTIVE_WINDOW_S = 30   # 最近 tool 事件 age 小于该秒数视为活跃
_ARGS_CAP = 120         # events.args 截断长度
_LIST_CAP = 200         # fs/list 条目上限
_SEARCH_FILE_CAP = 500  # search 扫描文件数上限
_SEARCH_HIT_CAP = 100   # search 命中上限
_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # search 单文件上限
_GIT_OUT_CAP = 8192     # git/status 输出截断

# fs/list 与 search 共同排除的目录
_EXCLUDE_DIRS = {".git", "node_modules", "target", "dist", "__pycache__"}

# 写类工具集合（agent/status.last_write 判定）
_WRITE_TOOLS = {"fs_write", "locate_edit", "patch_learn", "ide_actions"}
# 从 args 提取首个疑似绝对路径（Windows 盘符路径 或 POSIX 路径）
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\"'\s,]+|/[\w./-]+")


def _telemetry_path():
    """telemetry 路径：UNIFIED_RX_TELEMETRY 环境变量可覆盖，否则默认路径。"""
    override = os.environ.get("UNIFIED_RX_TELEMETRY", "")
    return override if override.strip() else _TELEMETRY_DEFAULT


# ---------- telemetry 尾部块读（仿 termlog 块边界处理，不全量读） ----------

def _tail_rows(path, count):
    """文件尾 count 个非空行的 [(offset, bytes)]（旧在前）。

    从文件尾按 _CHUNK 块倒读，维护跨块行首 carry；返回每行的起始字节偏移，
    feed 的重置 cursor 与增量读取都复用。文件不可读返回 []。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= 0:
        return []
    rows = []  # 新在前 (offset, bytes)
    carry = b""
    pos = size
    try:
        with open(path, "rb") as f:
            while pos > 0 and len(rows) < count:
                step = min(_CHUNK, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + carry
                parts = data.split(b"\n")
                carry = parts[0]
                off = pos + len(data)
                for p in reversed(parts[1:]):
                    off -= len(p) + 1
                    if p.strip():
                        rows.append((off, p))
                        if len(rows) >= count:
                            break
        if len(rows) < count and carry.strip():
            rows.append((pos, carry))  # 文件首行（pos==0）或块首完整行
    except OSError:
        return []
    rows.reverse()
    return rows


def _parse_event(raw):
    """telemetry 一行 → 事件 dict；解析失败返回 None。"""
    try:
        rec = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    ev = {"ts": rec.get("ts"), "kind": rec.get("kind")}
    for k in ("tool", "status", "wall_ms", "cycle_ms"):
        if rec.get(k) is not None:
            ev[k] = rec[k]
    if rec.get("args") is not None:
        a = rec["args"]
        s = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
        ev["args"] = s[:_ARGS_CAP]
    return ev


# ---------- 端点 1：fs/list ----------

def fs_list(path):
    """单层列举目录。绝对路径必须；排除 .git/node_modules/target/dist/
    __pycache__；目录在前按名排序；条目上限 200（超出 truncated:true）。"""
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "缺少 path"}
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        return {"ok": False, "error": "path 必须是绝对路径"}
    ap = os.path.normpath(os.path.abspath(path))  # 防 .. 穿越
    if not os.path.isdir(ap):
        return {"ok": False, "error": "不是目录: %s" % ap}
    try:
        names = os.listdir(ap)
    except OSError as e:
        return {"ok": False, "error": "列举失败: %s" % e}
    entries = []
    for name in names:
        if name in _EXCLUDE_DIRS:
            continue
        full = os.path.join(ap, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        entries.append({"name": name,
                        "type": "dir" if os.path.isdir(full) else "file",
                        "size": st.st_size,
                        "mtime": int(st.st_mtime)})
    # 目录在前，各自按名（不区分大小写）排序
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    truncated = len(entries) > _LIST_CAP
    return {"ok": True, "entries": entries[:_LIST_CAP], "truncated": truncated}


# ---------- 端点 2：search ----------

def search(query, root):
    """os.walk 递归子串搜索。排除 _EXCLUDE_DIRS；单文件 >2MB 跳过；
    扫描文件数上限 500、命中上限 100；二进制（首块含 \\0）跳过；
    text 截断 160 字符。query 空 → error。"""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "缺少 query"}
    root = os.path.expanduser((root or "").strip())
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "root 不是有效目录"}
    root = os.path.normpath(os.path.abspath(root))
    hits = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if scanned >= _SEARCH_FILE_CAP or len(hits) >= _SEARCH_HIT_CAP:
                break
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > _SEARCH_FILE_BYTES:
                    continue
                scanned += 1
                with open(full, "rb") as f:
                    if b"\0" in f.read(8192):
                        continue  # 二进制
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if query in line:
                            hits.append({"path": full, "line": i,
                                         "text": line.strip()[:160]})
                            if len(hits) >= _SEARCH_HIT_CAP:
                                break
            except OSError:
                continue
        if scanned >= _SEARCH_FILE_CAP or len(hits) >= _SEARCH_HIT_CAP:
            break
    return {"ok": True, "hits": hits}


# ---------- 端点 3：git/status ----------

def git_status(root):
    """git status --short（shell=False，timeout=10）。非零退出 → error。"""
    root = os.path.expanduser((root or "").strip())
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "root 不是有效目录"}
    try:
        r = subprocess.run(["git", "status", "--short"], shell=False,
                           capture_output=True, text=True, cwd=root,
                           timeout=10, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git 超时（>10s）"}
    except OSError as e:
        return {"ok": False, "error": "git 执行失败: %s" % e}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "git status 失败")[:2000]}
    return {"ok": True, "output": (r.stdout or "")[:_GIT_OUT_CAP]}


# ---------- 端点 4：agent/feed ----------

def agent_feed(cursor):
    """telemetry.jsonl 增量游标读取（字节偏移）。

    cursor<=0 → 回放文件尾最近 200 行；cursor>size（文件轮转/缩小）→
    同样重置为尾 200 行且 reset:true。响应 cursor 为新偏移（已消费到
    最后一个完整行）。解析失败行跳过。
    """
    path = _telemetry_path()
    if not os.path.isfile(path):
        return {"ok": True, "cursor": 0, "events": []}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"ok": True, "cursor": 0, "events": []}
    if cursor <= 0 or cursor > size:
        reset = cursor > 0
        rows = _tail_rows(path, _TAIL_CAP)
        events = [e for e in (_parse_event(raw) for _off, raw in rows)
                  if e is not None]
        out = {"ok": True, "cursor": size, "events": events}
        if reset:
            out["reset"] = True
        return out
    # 增量：读 [cursor, size)，只消费完整行（尾部半行留待下次）
    try:
        with open(path, "rb") as f:
            f.seek(cursor)
            data = f.read(min(size - cursor, 4 * 1024 * 1024))
    except OSError:
        return {"ok": True, "cursor": cursor, "events": []}
    idx = data.rfind(b"\n")
    if idx < 0:
        return {"ok": True, "cursor": cursor, "events": []}
    events = []
    for ln in data[:idx].split(b"\n"):
        if not ln.strip():
            continue
        e = _parse_event(ln)
        if e is not None:
            events.append(e)
    return {"ok": True, "cursor": cursor + idx + 1, "events": events}


# ---------- 端点 5：agent/status ----------

def agent_status():
    """智能体活跃状态：读 telemetry 尾 50 行。

    active = 最近 kind=tool 事件 age<30s；last_write = 尾部倒序第一个
    tool∈写类集合且 status=ok 的事件，path 从 args 提取首个疑似绝对路径；
    无则 null。
    """
    out = {"ok": True, "active": False, "last_event_age_s": 0,
           "last_write": None}
    path = _telemetry_path()
    if not os.path.isfile(path):
        return out
    recs = []
    for _off, raw in _tail_rows(path, _STATUS_TAIL):
        try:
            r = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if isinstance(r, dict):
            recs.append(r)
    if not recs:
        return out
    now = time.time()
    last_tool = next((r for r in reversed(recs)
                      if r.get("kind") == "tool"), None)
    if last_tool is not None:
        try:
            age = max(0, int(now - float(last_tool.get("ts") or 0)))
        except (TypeError, ValueError):
            age = 0
        out["last_event_age_s"] = age
        out["active"] = age < _ACTIVE_WINDOW_S
    write_ev = next((r for r in reversed(recs)
                     if r.get("kind") == "tool"
                     and r.get("tool") in _WRITE_TOOLS
                     and r.get("status") == "ok"), None)
    if write_ev is not None:
        args = write_ev.get("args")
        s = args if isinstance(args, str) else json.dumps(
            args or {}, ensure_ascii=False)
        m = _PATH_RE.search(s)
        out["last_write"] = {"tool": write_ev.get("tool"),
                             "path": m.group(0) if m else None,
                             "ts": write_ev.get("ts")}
    return out
