# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""locate_core — Qoder 式代码定位：自然语言/符号 → 具体修改位置（AI 引导）。

核心能力（对标 Qoder 的"分析仓库 + 告诉 AI 改代码的具体位置"）：
1. 语义定位：query（符号名/自然语言关键词/报错片段）→ 候选位置列表
   [{file, line, symbol, snippet, score, reason}]
2. AI 引导：对每个候选给出"为什么可能是这里"（符号名命中 / 内容命中 /
   行内关键词命中），供 AI 决定改哪里。
3. 复用 cb_index 的符号索引（.unified-rx-index/index.json），无索引时降级全扫。

Python 3.8+ 标准库零依赖。与 server.py 同目录部署。
"""

import json
import os
import re
import time

from pathlib import Path

_MAX_FILE = 1 << 20
_INDEX_DIR = ".unified-rx-index"
_SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".pytest_cache",
              "__pycache__", ".idea", ".vscode", "vendor", _INDEX_DIR, ".codebase-memory"}

_SYMBOL_PATTERNS = {
    ".py": re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)", re.M),
    ".rs": re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)|^(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)", re.M),
    ".go": re.compile(r"^func\s+(\w+)", re.M),
    ".ts": re.compile(r"^(?:export\s+)?(?:function|class|interface|type|const|let)\s+(\w+)", re.M),
    ".js": re.compile(r"^(?:export\s+)?(?:function|class)\s+(\w+)", re.M),
    ".gd": re.compile(r"^(?:func|class_name)\s+(\w+)", re.M),
}

# 分词：query 拆成小写 token（camelCase 拆分 + 下划线拆分 + 中文逐段）
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-龥]{2,}")


def _tokens(query: str) -> list[str]:
    q = query.lower()
    q = re.sub(r"([a-z])([A-Z])", r"\1 \2", q)  # camelCase -> words
    q = q.replace("_", " ").replace("-", " ").replace(".", " ")
    toks = [t for t in _TOKEN_RE.findall(q) if len(t) >= 2]
    # 中文整串拆 2-gram 滑动窗口（"修改沙盒路径校验" → 沙盒/盒路/路径/径校/校验）
    # 仅用于行级内容匹配；符号级匹配用整串（_raw_tokens）
    out: list[str] = []
    for t in toks:
        if re.fullmatch(r"[一-龥]{2,}", t):
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
        else:
            out.append(t)
    return out


def _raw_tokens(query: str) -> list[str]:
    """未拆分的中文整串 token（符号级匹配用）。"""
    q = query.lower()
    q = re.sub(r"([a-z])([A-Z])", r"\1 \2", q)
    q = q.replace("_", " ").replace("-", " ").replace(".", " ")
    return [t for t in _TOKEN_RE.findall(q) if len(t) >= 2]


def _load_index(root: str) -> dict | None:
    idx_path = os.path.join(root, _INDEX_DIR, "index.json")
    try:
        # security MEDIUM：JSON 大小上限 32MB 防 DoS；捕获 RecursionError（深嵌套）
        if os.path.getsize(idx_path) > 32 << 20:
            return None
        with open(idx_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return None
        return data["files"]
    except (OSError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        return None


def _index_stale(root: str, max_age: float = 3600.0) -> bool:
    """索引新鲜度检查：index.json mtime 超过 max_age（默认 1h）视为过期。

    locate 首次调用会先触发 cb_index 重建；若重建失败（只读/受限）则降级
    全文件扫描（正确性不依赖索引）。
    """
    try:
        idx_path = os.path.join(root, _INDEX_DIR, "index.json")
        if not os.path.exists(idx_path):
            return True
        return time.time() - os.path.getmtime(idx_path) > max_age
    except OSError:
        return True


def _ensure_index(root: str) -> dict | None:
    """确保索引新鲜：过期/缺失时尝试重建；返回索引 dict（失败可能为 None）。"""
    if not _index_stale(root):
        return _load_index(root)
    try:
        from cb_index_core import index_repo
        index_repo(root)
    except Exception:
        pass  # 重建失败 → 降级
    return _load_index(root)


def _iter_files(root: str, max_files: int):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in _SYMBOL_PATTERNS:
                continue
            fp = os.path.join(dirpath, name)
            try:
                if os.path.getsize(fp) > _MAX_FILE:
                    continue
            except OSError:
                continue
            yield fp, suffix
            count += 1
            if count >= max_files:
                return


def _symbol_positions(src: str, suffix: str) -> dict[str, int]:
    """符号名 → 定义行（1-based）。"""
    pat = _SYMBOL_PATTERNS.get(suffix)
    if not pat:
        return {}
    out: dict[str, int] = {}
    for m in pat.finditer(src):
        sym = m.group(1) or m.group(2)
        if sym:
            out.setdefault(sym, src.count("\n", 0, m.start()) + 1)
    return out


def locate(root: str, query: str, max_files: int = 200, limit: int = 10) -> dict:
    """定位 query 相关的代码位置。返回 {ok, query, candidates: [{file,line,symbol,snippet,score,reason}]}。"""
    root = str(Path(root).resolve())
    tokens = _tokens(query)
    raw_tokens = _raw_tokens(query)
    if not tokens:
        return {"ok": False, "query": query, "error": "query 太短或无可识别关键词", "candidates": []}

    index = _ensure_index(root)  # TTL 自动重建（过期才重建）+ 符号粗筛免读文件
    q_lower = query.lower().replace("_", "").replace("-", "")

    def _sym_strength(sym: str) -> int:
        s_lower = sym.lower().replace("_", "").replace("-", "")
        if q_lower in s_lower:
            return 3  # 完整 query 是符号子串
        if all(t in s_lower for t in raw_tokens):
            return 2  # 全部 token 命中（用未拆分的整串）
        return 1 if any(t in s_lower for t in raw_tokens) else 0

    # 阶段 1：索引粗筛——只命中符号的文件需要读全文（行号定位）；
    # 无索引或未命中的文件走行级扫描。符号命中文件跳过行级匹配（候选已够）。
    symbol_files: list[tuple[str, str, list[tuple[str, int]], str | None, dict | None]] = []  # (rel, suffix, [(sym, strength)], preloaded_src, index_symbols)
    line_candidates: list[tuple[str, str, str | None]] = []  # (rel, suffix, preloaded_src)
    files_scanned = 0

    for fp, suffix in _iter_files(root, max_files):
        files_scanned += 1
        rel = os.path.relpath(fp, root).replace("\\", "/")
        pre = None  # 预读的 src（无索引路径下已读，行级扫描复用）
        if index and rel in index:
            idx_syms = index[rel].get("symbols")
            if isinstance(idx_syms, dict):
                hit = sorted(((s, _sym_strength(s)) for s in idx_syms if _sym_strength(s) > 0),
                             key=lambda kv: -kv[1])[:10]
                if hit:
                    symbol_files.append((rel, suffix, hit, None, idx_syms))
                    continue  # 索引带行号：阶段 2a 只读命中行，免读全文
            elif isinstance(idx_syms, list):
                hit = sorted(((s, _sym_strength(s)) for s in idx_syms if _sym_strength(s) > 0),
                             key=lambda kv: -kv[1])[:10]
                if hit:
                    symbol_files.append((rel, suffix, hit, None, None))
                    continue  # 旧格式无行号：阶段 2a 读全文定位
        else:
            # 无索引（或文件不在索引）→ 读文件提取符号判 hit
            try:
                pre = open(fp, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            syms = _symbol_positions(pre, suffix)
            hit = sorted(((s, _sym_strength(s)) for s in syms if _sym_strength(s) > 0),
                         key=lambda kv: -kv[1])[:10]
            if hit:
                symbol_files.append((rel, suffix, hit, pre, None))
                continue
        line_candidates.append((rel, suffix, pre))

    # 阶段 2a：符号命中文件——索引带行号则只读命中行；否则读全文定位行号
    candidates: list[dict] = []
    for rel, suffix, hit, pre, idx_syms in symbol_files:
        if idx_syms is not None:
            # 索引有行号：只读命中行取 snippet（免读全文，IO 最优）
            fp = os.path.join(root, *rel.split("/"))
            line_src: dict[int, str] = {}
            try:
                # security MEDIUM：open 前复核大小（索引可能引用遍历后膨胀的文件）
                if os.path.getsize(fp) > _MAX_FILE:
                    continue
                target_lines = {idx_syms.get(sym, 0) for sym, _ in hit[:10] if isinstance(idx_syms.get(sym, 0), int) and idx_syms.get(sym, 0) > 0}
                if not target_lines:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    # 行号炸弹防护：只读到最大目标行即停；行号超文件实际行数则部分缺失（不读全文件）
                    max_target = max(target_lines)
                    for i, ln in enumerate(f, start=1):
                        if i in target_lines:
                            line_src[i] = ln.strip()[:160]
                        if i >= max_target:
                            break
            except OSError:
                continue
            for sym, strength in hit[:10]:
                line = idx_syms.get(sym, 0)
                snippet = line_src.get(line, "")
                score = 200 if strength == 3 else (150 if strength == 2 else 60)
                candidates.append({
                    "file": rel, "line": line, "symbol": sym, "snippet": snippet,
                    "score": score, "reason": f"符号名命中: {sym}",
                })
            continue
        if pre is None:
            fp = os.path.join(root, *rel.split("/"))
            try:
                pre = open(fp, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
        lines = pre.splitlines()
        symbols = _symbol_positions(pre, suffix)
        for sym, strength in hit[:10]:
            line = symbols.get(sym, 0)
            snippet = (lines[line - 1].strip()[:160] if 0 < line <= len(lines) else "")
            score = 200 if strength == 3 else (150 if strength == 2 else 60)
            candidates.append({
                "file": rel, "line": line, "symbol": sym, "snippet": snippet,
                "score": score, "reason": f"符号名命中: {sym}",
            })

    # 阶段 2b：其余文件行级关键词匹配（每文件最多 3 条）
    for rel, suffix, pre in line_candidates:
        if pre is None:
            fp = os.path.join(root, *rel.split("/"))
            try:
                # security MEDIUM：open 前复核大小（防 TOCTOU 膨胀）
                if os.path.getsize(fp) > _MAX_FILE:
                    continue
                pre = open(fp, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
        lines = pre.splitlines()
        count = 0
        for i, ln in enumerate(lines[: len(lines)], start=1):
            low = ln.lower()
            if any(t in low for t in tokens):
                candidates.append({
                    "file": rel, "line": i, "symbol": "", "snippet": ln.strip()[:160],
                    "score": 50, "reason": "行内容关键词命中",
                })
                count += 1
                if count >= 3:
                    break

    # 去重（同一 file:line 只留最高分）
    seen: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["file"], c["line"])
        if key not in seen or c["score"] > seen[key]["score"]:
            seen[key] = c
    ranked = sorted(seen.values(), key=lambda c: -c["score"])[:limit]

    return {
        "ok": True,
        "query": query,
        "files_scanned": files_scanned,
        "candidates": ranked,
        "hint": "AI 引导：按 score 从高到低检查 candidate，修改位置以 file:line 为准；"
                "改前用 cae_code_context 取符号级上下文，改后跑 cae_change_impact 验证影响。",
    }
