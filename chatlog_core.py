#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""chatlog_core.py — 不同智能体聊天记录/留痕采集与检索（2026-08-17）。

用户要求（2026-08-17）："一定要获取不同的智能体聊天记录" +
"怎么知道有其他的智能体调用过——会在那个项目当中做笔记的"。

Adapter 模式（每种智能体一个采集器，格式不同互不影响）：

| agent | 来源 | 格式 |
|---|---|---|
| marvis | ~/.marvis/messages/*.md | YAML frontmatter（title/created_time/meta.prompt）+ markdown 正文 |
| hermes | <Hermes>/data/hermes-home/memories/*.md | Claude Code 风格记忆文件（MEMORY.md/USER.md） |
| trae   | %APPDATA%\\Trae CN\\User\\History/*/entries.json | VSCode 文件编辑历史（resource+时间戳） |
| qoder  | %APPDATA%\\Qoder\\User\\History/*/entries.json | VSCode 文件编辑历史 |

统一索引：~/.unified-rx/chatlog.jsonl（追加式，按 (agent, hash(text)) 去重）。
"""
import datetime
import hashlib
import json
import os
import re
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".unified-rx")
CHATLOG = os.path.join(STATE_DIR, "chatlog.jsonl")

# Hermes 安装位置（HERMES_HOME 可覆盖）
_HERMES_HOME = (os.environ.get("HERMES_HOME")
                or r"D:\rj\AI\Hermes Agent CN Desktop\data\hermes-home")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _existing_keys() -> set:
    keys: set = set()
    try:
        with open(CHATLOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    keys.add((d.get("agent"), d.get("hash")))
                except ValueError:
                    continue
    except OSError:
        pass
    return keys


def _append(records: list[dict]) -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    existing = _existing_keys()
    added = 0
    with open(CHATLOG, "a", encoding="utf-8") as f:
        for r in records:
            key = (r["agent"], r["hash"])
            if key in existing:
                continue
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            existing.add(key)
            added += 1
    return added


# ── adapters ───────────────────────────────────────────────────────────

def _ad_marvis() -> list[dict]:
    """~/.marvis/messages/*.md：YAML frontmatter + 正文。"""
    base = os.path.join(os.path.expanduser("~"), ".marvis", "messages")
    records = []
    if not os.path.isdir(base):
        return records
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".md"):
            continue
        try:
            text = open(os.path.join(base, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        title, created, prompt = fn, "", ""
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        body = text
        if m:
            fm, body = m.group(1), m.group(2)
            tm = re.search(r"^title:\s*(.+)$", fm, re.M)
            cm = re.search(r"^created_time:\s*[\"']?([^\"'\n]+)", fm)
            pm = re.search(r'"prompt"\s*:\s*"((?:\\.|[^"\\]){0,500})"', fm)  # 长度上限防 ReDoS
            if tm:
                title = tm.group(1).strip()
            if cm:
                created = cm.group(1).strip()
            if pm:
                prompt = pm.group(1).encode().decode("unicode_escape", errors="replace")
        body = body.strip()
        if not body:
            body = prompt or "(空)"
        records.append({
            "agent": "marvis", "source": f"~/.marvis/messages/{fn}",
            "ts": _parse_ts(created), "title": title[:200],
            "text": body[:4000], "hash": _hash(body),
        })
    return records


def _ad_hermes() -> list[dict]:
    """<Hermes>/memories/*.md：记忆/备忘录文件。"""
    base = os.path.join(_HERMES_HOME, "memories")
    records = []
    if not os.path.isdir(base):
        return records
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".md"):
            continue
        try:
            text = open(os.path.join(base, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not text.strip():
            continue
        st = os.stat(os.path.join(base, fn))
        records.append({
            "agent": "hermes", "source": f"<Hermes>/memories/{fn}",
            "ts": st.st_mtime, "title": fn, "text": text[:4000],
            "hash": _hash(text),
        })
    return records


def _ad_editor_history(agent: str, base: str) -> list[dict]:
    """VSCode 系 History/*/entries.json：文件编辑留痕（哪个智能体改过哪些文件）。"""
    records = []
    if not os.path.isdir(base):
        return records
    for d in sorted(os.listdir(base)):
        ep = os.path.join(base, d, "entries.json")
        if not os.path.isfile(ep):
            continue
        try:
            data = json.load(open(ep, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        resource = data.get("resource", "")
        for e in data.get("entries", [])[:5]:  # 每文件最多 5 条历史
            records.append({
                "agent": agent, "source": ep,
                "ts": e.get("timestamp", 0) / 1000.0,
                "title": f"编辑 {os.path.basename(resource)}",
                "text": f"{resource}\n来源: {e.get('source', '')}",
                "hash": _hash(f"{resource}:{e.get('id', '')}"),
            })
    return records


def _parse_ts(s: str) -> float:
    if not s:
        return time.time()
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return time.time()


def collect(agents: list[str] | None = None) -> dict:
    """采集指定智能体（默认全部）→ chatlog.jsonl（去重追加）。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    ap = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    adapters = {
        "marvis": _ad_marvis,
        "hermes": _ad_hermes,
        "trae": lambda: _ad_editor_history("trae", os.path.join(ap, "Trae CN", "User", "History")),
        "qoder": lambda: _ad_editor_history("qoder", os.path.join(ap, "Qoder", "User", "History")),
    }
    targets = agents or list(adapters)
    per_agent: dict[str, int] = {}
    total_new = 0
    for name in targets:
        if name not in adapters:
            continue
        try:
            recs = adapters[name]()
        except Exception:
            recs = []
        n = _append(recs)
        per_agent[name] = {"found": len(recs), "new": n}
        total_new += n
    # 统计总量
    total = 0
    try:
        with open(CHATLOG, encoding="utf-8") as f:
            total = sum(1 for ln in f if ln.strip())
    except OSError:
        pass
    return {"ok": True, "added": total_new, "total": total,
            "per_agent": per_agent, "chatlog": CHATLOG}


def search(query: str, agent: str | None = None, limit: int = 20,
           since_days: int | None = None) -> dict:
    """关键词检索 chatlog 索引（大小写不敏感，任意子串匹配）。"""
    q = query.lower()
    hits = []
    cutoff = (time.time() - since_days * 86400) if since_days else None
    try:
        with open(CHATLOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if agent and d.get("agent") != agent:
                    continue
                if cutoff and d.get("ts", 0) < cutoff:
                    continue
                hay = f"{d.get('title', '')} {d.get('text', '')}".lower()
                if q and q not in hay:
                    continue
                hits.append(d)
                if len(hits) >= limit:
                    break
    except OSError:
        pass
    return {"ok": True, "query": query, "agent": agent, "hits": hits,
            "count": len(hits), "chatlog": CHATLOG}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        print(json.dumps(search(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False, indent=2)[:3000])
    else:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
