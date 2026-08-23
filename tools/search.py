# -*- coding: utf-8 -*-
"""tools/search.py —— 语义检索域（2 工具）：code_search / kb_query

收敛自旧版 code_search(BM25) + explore_code/semantic_search/dep_graph/kb_query。
纯 stdlib BM25（无 Rust 依赖，零嵌入模型）——检索质量够用且本地秒级。
"""
import os
import re
import math
import json

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


def _bm25(idx, doc_len, doc_paths, query, k=1.5, b=0.75):
    q_toks = _tokenize(query)
    if not q_toks:
        return []
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
    return [(fp, s) for fp, s in ranked if s > 0]


@tool("code_search", "语义代码检索（BM25 符号加权：中文/英文/标识符 → 文件:行）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言/中文/符号查询"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "k": {"type": "integer", "description": "返回条数（默认 10）"},
       },
       "required": ["query"]})
def code_search(query, root=None, k=10):
    root = root or os.getcwd()
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    idx, doc_len, doc_paths = _index(root, _MAX_FILES)
    ranked = _bm25(idx, doc_len, doc_paths, query)
    hits = []
    for fp, score in ranked[:k]:
        # 找命中行
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        best_line, best_score = 1, 0
        q_toks = set(_tokenize(query))
        for i, line in enumerate(lines, 1):
            lt = set(_tokenize(line))
            hit = len(lt & q_toks)
            if hit > best_score:
                best_score, best_line = hit, i
        hits.append({"file": fp, "line": best_line, "score": round(score, 3),
                     "snippet": lines[best_line - 1].strip()[:120] if lines else ""})
    return {"query": query, "total": len(hits), "hits": hits}


@tool("kb_query", "知识库/文档混合检索（同 BM25 引擎，懒建索引）", "search",
      {"type": "object",
       "properties": {
           "index_dir": {"type": "string", "description": "源码/知识库目录"},
           "query": {"type": "string", "description": "检索词"},
           "limit": {"type": "integer", "description": "返回条数（默认 10）"},
       },
       "required": ["index_dir", "query"]})
def kb_query(index_dir, query, limit=10):
    if not os.path.isdir(index_dir):
        return {"error": f"不是目录: {index_dir}"}
    idx, doc_len, doc_paths = _index(index_dir, _MAX_FILES)
    ranked = _bm25(idx, doc_len, doc_paths, query)
    hits = []
    for fp, score in ranked[:limit]:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(400)
        except OSError:
            head = ""
        hits.append({"file": fp, "score": round(score, 3), "head": head.strip()[:120]})
    return {"query": query, "total": len(hits), "hits": hits}
