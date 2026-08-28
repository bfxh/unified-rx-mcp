# -*- coding: utf-8 -*-
"""tools/search.py —— 语义检索域（1 工具）：code_search

收敛自旧版 code_search(BM25) + explore_code/semantic_search/dep_graph/kb_query；
kb_query 于 S15 移除（同引擎重复面，L3 实战 100+ 会话零调用）。
纯 stdlib BM25（无 Rust 依赖，零嵌入模型）——检索质量够用且本地秒级。
"""
import os
import re
import math
from collections import Counter

from registry import tool

_MAX_FILES = 200
_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "of", "in", "on",
              "at", "to", "from", "and", "or", "for", "with", "this", "that",
              "it", "as", "by", "be", "been", "being", "have", "has", "had",
              "do", "does", "did", "但", "是", "的", "了", "在", "与", "和", "或"}


def _tokenize(text):
    """标识符拆词：camelCase / snake_case / 中文 bigram。"""
    tokens = []
    # 英文标识符拆词
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
        w = m.group(0)
        # camelCase / PascalCase 拆分
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", w)
        tokens.extend(p.lower() for p in parts if len(p) > 1)
        tokens.append(w.lower())
    # 中文 bigram
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        seg = m.group(0)
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
            tokens.append(seg)
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _index(root, max_files):
    """构建倒排索引：token → [(file, count)]。返回 (idx, doc_len, doc_paths)。"""
    idx = {}
    doc_len = {}
    doc_paths = []
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build",
                                                ".unified-rx-index", "backups")]
        for fn in files:
            if count >= max_files:
                break
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
                           ".gd", ".cs", ".dart", ".lua", ".java", ".kt", ".md",
                           ".toml", ".json", ".yaml", ".yml"):
                continue
            count += 1
            fp = os.path.join(r, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            doc_paths.append(fp)
            toks = _tokenize(src)
            doc_len[fp] = len(toks)
            seen = {}
            for t in toks:
                seen[t] = seen.get(t, 0) + 1
            for t, c in seen.items():
                idx.setdefault(t, []).append((fp, c))
    return idx, doc_len, doc_paths


def _fingerprints(root):
    """root 下参与索引文件的指纹表 {path: (mtime_ns, size)}——只 walk+stat，不读内容。"""
    fps = {}
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build",
                                                ".unified-rx-index", "backups")]
        for fn in files:
            if count >= _MAX_FILES:
                break
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _INDEX_EXTS:
                continue
            count += 1
            fp = os.path.join(r, fn)
            try:
                st = os.stat(fp)
                fps[fp] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return fps


# S12：BM25 索引指纹缓存——重复查询免去 200 文件全量读解析（实测 VF3 首查 ~0.4s → 复查 <10ms）
_IDX_CACHE = {}     # root -> {"key": hash, "data": (idx, doc_len, doc_paths)}
_IDX_CACHE_MAX = 4
_IDX_CACHE_ORDER = []


def _get_index(root):
    fps = _fingerprints(root)
    key = hash(tuple(sorted(fps.items())))
    ent = _IDX_CACHE.get(root)
    if ent is not None and ent["key"] == key:
        return ent["data"]
    data = _index(root, _MAX_FILES)
    if root not in _IDX_CACHE_ORDER:
        _IDX_CACHE_ORDER.append(root)
        while len(_IDX_CACHE_ORDER) > _IDX_CACHE_MAX:
            old = _IDX_CACHE_ORDER.pop(0)
            _IDX_CACHE.pop(old, None)
    _IDX_CACHE[root] = {"key": key, "data": data}
    return data


_INDEX_EXTS = frozenset((
    ".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
    ".gd", ".cs", ".dart", ".lua", ".java", ".kt", ".md",
    ".toml", ".json", ".yaml", ".yml"))


def _bm25(idx, doc_len, doc_paths, query, k=1.5, b=0.75):
    q_toks = _tokenize(query)
    if not q_toks:
        return []
    # S13 准确率：查询的原始词形（连续符号，如 load_module_defs）——用于命中行加权重排
    raw_terms = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", query)}
    raw_terms |= set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    N = max(1, len(doc_paths))
    avgdl = sum(doc_len.values()) / max(1, len(doc_len))
    scores = {}
    for t in set(q_toks):
        posts = idx.get(t, [])
        df = len(posts)
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for fp, tf in posts:
            dl = doc_len.get(fp, 1)
            denom = tf + k * (1 - b + b * dl / max(1, avgdl))
            scores[fp] = scores.get(fp, 0) + idf * tf / denom
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [(fp, s) for fp, s in ranked if s > 0], raw_terms


@tool("code_search", "语义代码检索（BM25 符号加权：中文/英文/标识符 → 文件:行）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言/中文/符号查询"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "k": {"type": "integer", "description": "返回条数（默认 10）"},
       },
       "required": ["query"]})
def code_search(query, root=None, k=10):
    root = os.path.abspath(root or os.getcwd())
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    idx, doc_len, doc_paths = _get_index(root)
    ranked, raw_terms = _bm25(idx, doc_len, doc_paths, query)
    hits = []
    cache = {}      # S13：fp→lines 复用（同一文件多命中免重复读盘）
    for fp, score in ranked[:k * 2]:
        try:
            lines = cache.get(fp)
            if lines is None:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                cache[fp] = lines
        except OSError:
            continue
        best_line, best_score = 1, 0
        q_toks = set(_tokenize(query))
        for i, line in enumerate(lines, 1):
            lt = set(_tokenize(line))
            hit = len(lt & q_toks)
            low = line.lower()
            # 连续符号精确出现（raw term 原文在行内）给足额加分 → 精确符号置顶
            for rt in raw_terms:
                if rt in low:
                    hit += 6
                    break
            if hit > best_score:
                best_score, best_line = hit, i
        if best_score == 0 and len(hits) >= k:
            continue
        hits.append({"file": fp, "line": best_line, "score": round(score, 3),
                     "snippet": lines[best_line - 1].strip()[:120] if lines else ""})
        if len(hits) >= k:
            break
    return {"query": query, "total": len(hits), "hits": hits}




# ================= S31：code_semantic —— 符号级向量空间语义检索 =================
# 与 BM25（文件级关键词相关）互补：定义级 tf-idf 余弦向量，按语义相邻排序。
# 零依赖离线：identifier 拆词 + 名称 char-trigram + 定义体 token，纯 dict 运算。

_SEM_DEF_RE = [
    # (lang, 正则, 符号类型)
    ("py", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)"), "def"),
    ("rs", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), "fn"),
    ("rs", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)"), "type"),
    ("rs", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:\w+\s+for\s+)?(\w+)"), "impl"),
    ("go", re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"), "fn"),
    ("js", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "fn"),
    ("js", re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class"),
]
_SEM_BODY_CAP = 40          # 定义体 token 采样行数
_SEM_MAX_DEFS = 4000


def _sem_defs(root, max_files):
    """提取符号定义文档：[(file, line, kind, name, body_text)]。"""
    defs = []
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build",
                                                ".unified-rx-index", "backups")]
        for fn in files:
            if count >= max_files or len(defs) >= _SEM_MAX_DEFS:
                return defs
            if os.path.splitext(fn)[1].lower() not in _INDEX_EXTS:
                continue
            count += 1
            fp = os.path.join(r, fn)
            lang = os.path.splitext(fn)[1].lower().lstrip(".")
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                for lg, pat, kind in _SEM_DEF_RE:
                    if lg != lang:
                        continue
                    m = pat.match(line)
                    if not m:
                        continue
                    name = next((g for g in m.groups() if g), "")
                    if not name:
                        continue
                    # 定义上方紧邻注释行纳入 body（doc comment 是语义信号）
                    start = i
                    while start > 0 and re.match(
                            r"^\s*(//|#|\"\"\"|''')", lines[start - 1]):
                        start -= 1
                    body = "\n".join(lines[start:i + _SEM_BODY_CAP])
                    defs.append({"file": fp, "line": i + 1, "kind": kind,
                                 "name": name, "body": body})
                    break
    return defs


def _sem_vec(text, name, idf):
    """tf-idf 权重 dict：名称 token ×3 + 名称 trigram ×2 + 定义体 ×1。"""
    tf = {}
    for t in _tokenize(name):
        tf[t] = tf.get(t, 0) + 3
    for j in range(len(name) - 2):
        g = name[j:j + 3].lower()
        tf[g] = tf.get(g, 0) + 2
    for t in _tokenize(text):
        tf[t] = tf.get(t, 0) + 1
    return {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}


def _cosine(a, b):
    if len(b) < len(a):
        a, b = b, a
    dot = sum(v * b.get(t, 0.0) for t, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


_SEM_CACHE = {}


def _get_sem_index(root):
    fps = _fingerprints(root)
    key = hash(tuple(sorted(fps.items())))
    ent = _SEM_CACHE.get(root)
    if ent is not None and ent["key"] == key:
        return ent["data"]
    defs = _sem_defs(root, _MAX_FILES)
    # idf：N/df 的平滑对数（0.4 权重低频词，避免单点爆炸）
    df = Counter()
    for d in defs:
        df.update(set(_tokenize(d["name"])) | set(_tokenize(d["body"])))
    n = max(1, len(defs))
    idf = {t: 0.4 + 0.6 * math.log(1 + n / (1 + c)) for t, c in df.items()}
    for d in defs:
        d["vec"] = _sem_vec(d["body"], d["name"], idf)
    data = (defs, idf)
    _SEM_CACHE[root] = {"key": key, "data": data}
    return data


@tool("code_semantic", "向量空间语义检索：自然语言 → 符号定义（tf-idf 余弦，"
      "mode=search 找定义 / mode=related 找语义相邻符号）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言（search）或符号名（related）"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "mode": {"type": "string", "enum": ["search", "related"],
                    "description": "search=语义找定义；related=给定符号的语义邻居"},
           "k": {"type": "integer", "description": "返回条数（默认 8）"},
       },
       "required": ["query"]})
def code_semantic(query, root=None, mode="search", k=8):
    root = os.path.abspath(root or os.getcwd())
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    defs, idf = _get_sem_index(root)
    if not defs:
        return {"query": query, "total": 0, "hits": []}
    if mode == "related":
        target = next((d for d in defs if d["name"] == query), None)
        if target is None:            # 模糊：最高余弦的定义当锚点
            qv = _sem_vec(query, query, idf)
            best = max(defs, key=lambda d: _cosine(qv, d["vec"]))
            target = best
        ranked = sorted((d for d in defs if d is not target),
                        key=lambda d: -_cosine(target["vec"], d["vec"]))
        hits = [{"file": d["file"], "line": d["line"], "symbol": d["name"],
                 "kind": d["kind"], "score": round(_cosine(target["vec"], d["vec"]), 3)}
                for d in ranked[:k] if _cosine(target["vec"], d["vec"]) > 0.05]
        return {"query": query, "mode": "related", "anchor": target["name"],
                "total": len(hits), "hits": hits}
    qv = _sem_vec(query, query, idf)
    ranked = sorted(defs, key=lambda d: -_cosine(qv, d["vec"]))
    hits = []
    for d in ranked[:k]:
        s = _cosine(qv, d["vec"])
        if s <= 0.02:
            break
        snippet = ""
        try:
            with open(d["file"], "r", encoding="utf-8", errors="replace") as f:
                snippet = f.readlines()[d["line"] - 1].strip()[:120]
        except OSError:
            pass
        hits.append({"file": d["file"], "line": d["line"], "symbol": d["name"],
                     "kind": d["kind"], "score": round(s, 3), "snippet": snippet})
    return {"query": query, "mode": "search", "total": len(hits), "hits": hits}
