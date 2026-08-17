import sys as _sys
for _m in ['locate_core', 'causal_debug', 'lse_client']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])

"""locate_engine — 定位引擎。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""


# ══════════════ locate_core（合并） ══════════════
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

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

    # IDE 增强 290：定位候选语言分布（候选文件后缀——AI 知道
    # 相关代码的语言，对称扫描工具 languages）
    _l_langs: dict[str, int] = {}
    for _c in ranked:
        _sfx = os.path.splitext(str(_c.get("file", "")))[1].lower().lstrip(".")
        if _sfx:
            _l_langs[_sfx] = _l_langs.get(_sfx, 0) + 1
    return {
        "ok": True,
        "query": query,
        "files_scanned": files_scanned,
        "languages": dict(sorted(_l_langs.items(), key=lambda kv: -kv[1])),
        "candidates": ranked,
        "hint": "AI 引导：按 score 从高到低检查 candidate，修改位置以 file:line 为准；"
                "改前用 cae_code_context 取符号级上下文，改后跑 cae_change_impact 验证影响。",
    }
# ══════════════ causal_debug（合并） ══════════════
# -*- coding: utf-8 -*-
"""causal_debug —— 因果建模与调试（2026-08-15，阶段1）。

不再是找"哪里错了"，而是问"为什么错"：
① causal_trace：事件因果链——scan-log 调用记录 + git 提交 → 失败事件
  → 溯源到引入它的 Agent 行为/工具调用（"是哪个行为导致构建失败"）
② bug_bisect：git bisect 式二分——自动化二分查找引入 bug 的提交
  （真实 git 操作——只读：log/rev-list/checkout 由调用方确认）

全部只读/建议层——不自动改代码。
"""
import subprocess


# ── ① 因果溯源 ─────────────────────────────────────────────
def causal_trace(root: str, fail_keyword: str = "fail",
                 limit: int = 200) -> dict:
    """因果溯源：失败事件 → 回溯因果链（最近的代码变更 + 工具调用）。

    数据源：
    - git log（最近提交——代码变更因果）
    - scan-log（工具调用记录——Agent 行为因果）
    输出：候选原因链（按时间倒序）——"先看哪个变更/行为"
    """
    chain: list[dict] = []
    # A. git 提交因果（最近 20 条——含作者/消息/时间）
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", "-20"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    chain.append({"kind": "commit", "hash": parts[0][:10],
                                  "author": parts[1], "time": parts[2],
                                  "message": parts[3][:100]})
    except (OSError, subprocess.TimeoutExpired):
        pass  # 非 git 项目/超时——跳过提交因果
    # B. scan-log 工具调用因果（最近调用——Agent 行为）
    try:
        import scan_log_core
        logs = scan_log_core.query_logs(limit=limit)
        for l in logs[-30:]:
            chain.append({"kind": "tool_call",
                          "tool": l.get("tool", ""),
                          "root": str(l.get("root", ""))[:60],
                          "summary": str(l.get("summary", ""))[:100],
                          "time": l.get("ts", "")})
    except Exception:  # 尽力而为
        pass
    # C. 失败事件定位（最近失败记录）
    fails = []
    try:
        import scan_log_core
        for l in scan_log_core.query_logs(limit=limit):
            sm = str(l.get("summary", ""))
            if fail_keyword.lower() in sm.lower() or l.get("ok") is False:
                fails.append({"tool": l.get("tool", ""),
                              "summary": sm[:100],
                              "time": l.get("ts", "")})
    except Exception:  # 尽力而为
        pass
    # 因果结论：失败前最近的变更/行为（倒序链前 10 条）
    return {"ok": True, "root": root, "fail_keyword": fail_keyword,
            "fail_events": fails[:10],
            "causal_chain": chain[:15],
            "advice": ("因果溯源：失败事件发生前最近的提交/工具调用是首要嫌疑"
                       "（链首）——用 bug_bisect 二分确认引入提交；"
                       "用 predict_impact 预测修复影响面")}


# ── ② git 二分定位 ─────────────────────────────────────────
def bug_bisect(root: str, good_commit: str, bad_commit: str,
               test_cmd: str, max_steps: int = 15,
               execute: bool = False) -> dict:
    """git bisect 式二分：在 [good, bad] 区间二分查找引入 bug 的提交。

    execute=False（默认）：只读计划（rev-list 计数 + mid 建议——不 checkout）。
    execute=True：实际执行二分（checkout mid 提交 → 跑 test_cmd → 收缩区间）
    ——写操作（checkout）受 L4 授权（调用方显式确认）。
    实现用 git bisect 原生命令（start/bad/good/run——不手写二分循环）。
    """
    import subprocess as _sp
    # execute 路径：git bisect 原生（start bad good → run test_cmd）
    if execute:
        # security-review HIGH（遗漏修复）：test_cmd 精确白名单——防任意命令执行
        _ALLOWED = ("cargo test", "cargo check", "python -m pytest", "pytest",
                    "node --test", "npm test", "go test", "go vet")
        import re as _re
        if not any(test_cmd == k or
                   (k == "cargo test" and test_cmd.startswith("cargo test "))
                   or (k == "python -m pytest" and test_cmd.startswith("python -m pytest "))
                   or (k == "pytest" and test_cmd.startswith("pytest "))
                   for k in _ALLOWED):
            return {"ok": False, "error": f"test_cmd 不在白名单: {test_cmd!r}"}
        # 终审 MEDIUM：参数 token 仅允许标识符/路径字符（白名单自带 -m/--test 豁免）
        _fixed = set()
        if test_cmd.startswith("python -m pytest"):
            _fixed = {"-m"}
        elif test_cmd.startswith("node --test"):
            _fixed = {"--test"}
        if not all(_re.match(r"^(?!-)[A-Za-z0-9_\-./]+$", t)
                   for t in test_cmd.split()[1:] if t not in _fixed):
            return {"ok": False, "error": f"test_cmd 参数含非法字符: {test_cmd!r}"}
        try:
            # 重置可能的旧 bisect 状态
            _sp.run(["git", "-C", root, "bisect", "reset"],
                    capture_output=True, text=True, timeout=15)
            r = _sp.run(["git", "-C", root, "bisect", "start",
                         bad_commit, good_commit],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return {"ok": False, "error": f"bisect start 失败: {r.stderr[:120]}"}
            try:
                r = _sp.run(["git", "-C", root, "bisect", "run",
                             *test_cmd.split()],
                            capture_output=True, text=True, timeout=600)
                out = (r.stdout or "") + (r.stderr or "")
                first_bad = _extract_first_bad(out)
            finally:
                # 安全（security-review MEDIUM）：无论结果/异常都恢复 HEAD
                # ——防超时/异常后工作区停在 mid 提交 + BISECT 状态残留
                _sp.run(["git", "-C", root, "bisect", "reset"],
                        capture_output=True, text=True, timeout=15)
            return {"ok": True, "executed": True,
                    "first_bad_commit": first_bad,
                    "log_tail": out[-500:],
                    "advice": f"引入 bug 的提交: {first_bad or '未定位（测试命令退出码语义检查）'}"
                              "——修复后 causal_trace 溯源行为链"}
        except (OSError, _sp.TimeoutExpired) as e:
            return {"ok": False, "error": f"bisect 执行失败: {e}"}
    # 只读计划路径（原行为）
    try:
        r = subprocess.run(
            ["git", "-C", root, "rev-list", "--count", f"{good_commit}..{bad_commit}"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "error": f"rev-list 失败: {r.stderr[:100]}"}
        total = int(r.stdout.strip())
        if total <= 0:
            return {"ok": False, "error": "区间无提交（good/bad 顺序或范围错误）"}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"ok": False, "error": f"git 不可用: {e}"}
    # 二分计划（不实际 checkout——建议层）
    mid = total // 2
    plan = (f"区间 {good_commit[:8]}..{bad_commit[:8]} 共 {total} 个提交——"
            f"二分第 1 步：checkout 第 {mid} 个提交（约一半）跑 {test_cmd[:40]}，"
            f"按结果收缩区间——最多 {max_steps} 步定位引入提交")
    return {"ok": True, "total_commits": total, "mid_index": mid,
            "max_steps": min(max_steps, total.bit_length()),
            "next": plan,
            "advice": "加 execute=true 实际执行 git bisect（L4 授权——会 checkout）；"
                      "确认定位后：修复提交 + causal_trace 溯源行为链"}


def _extract_first_bad(output: str) -> str | None:
    """从 git bisect run 输出提取 'first bad commit' 的 hash。"""
    m = re.search(r"first bad commit:\s*\[?([0-9a-f]{7,40})", output)
    if m:
        return m.group(1)
    return None


# ── ③ 因果链记录（scan-log 扩展）──────────────────────────
def record_cause(root: str, effect: str, cause: str) -> dict:
    """记录因果链（cause → effect——scan-log tool=causal_link）。

    供 Agent 行为链回放：哪个行为（cause）导致了什么结果（effect）。
    """
    try:
        import scan_log_core
        scan_log_core.append_scan({
            "tool": "causal_link", "root": root, "ok": True,
            "summary": f"因果: {cause[:60]} → {effect[:60]}"})
        return {"ok": True, "cause": cause, "effect": effect,
                "log": "因果链已入 scan-log（tool=causal_link 可查）"}
    except Exception as e:  # 尽力而为
        return {"ok": False, "error": str(e)[:80]}
# ══════════════ lse_client（合并） ══════════════
"""lse_client — LSE 引擎的 Python 客户端（subprocess JSON 协议）。

调用 Rust lse-engine（~/.unified-rx/lse-state.json 持久化）：
- delta_update_lesson(id, delta): 教训效用分更新（Delta 奖励）
- delta_update_rule(id, delta, adopted): 规则权重更新（自适应）
- ucb_select(parent, children, c): UCB 树搜索分支选择
- ucb_backprop(id, reward): 树节点结果回流
- experience_store(model, ctx, delta, summary): 经验入库
- experience_match(ctx, limit): 按上下文指纹匹配经验

零第三方依赖（subprocess + json）。lse-engine exe 路径：同目录 ../lse-engine/target/release/lse-engine.exe

P1c 升级（2026-08-12，抄 mem0 自动提取 + Letta 三层）：
- lesson_store_tiered: 三层存教训（core/work/archive）
- auto_extract_lessons: 规则版自动教训提取（信号词匹配，零 LLM）
"""

import hashlib

_ENGINE_ROOT_P = Path(_ENGINE_ROOT)
_ENGINE_CANDIDATES = [
    # 同仓库 lse-engine 子目录 release 构建（unified-rx/lse-engine/target/release/）
    _ENGINE_ROOT_P / "lse-engine" / "target" / "release" / "lse-engine.exe",
    _ENGINE_ROOT_P / "lse-engine" / "target" / "release" / "lse-engine",
    # cargo 显式指定 build.target 时的布局（target/<三元组>/release/）
    _ENGINE_ROOT_P / "lse-engine" / "target" / "x86_64-pc-windows-gnu" / "release" / "lse-engine.exe",
    _ENGINE_ROOT_P / "lse-engine" / "target" / "x86_64-pc-windows-msvc" / "release" / "lse-engine.exe",
    # 仓库根（mcp-servers/ 布局）lse-engine
    _ENGINE_ROOT_P / "unified-rx" / "lse-engine" / "target" / "release" / "lse-engine.exe",
    # 环境变量覆盖
    Path(os.environ.get("LSE_ENGINE", "")) if os.environ.get("LSE_ENGINE") else None,
]

_TIMEOUT = 5.0


def _engine_path() -> Path | None:
    for c in _ENGINE_CANDIDATES:
        if c is not None and c.exists():
            return c
    return None


def _call(cmd: str, payload: dict) -> dict:
    """调用 lse-engine，返回 {ok, result|error}。引擎不可用时返回 ok:false。"""
    exe = _engine_path()
    if exe is None:
        return {"ok": False, "error": "lse-engine 未构建（lse-engine/target/release/）"}
    try:
        line = json.dumps({"cmd": cmd, "payload": payload}, ensure_ascii=False)
        proc = subprocess.run(
            [str(exe)], input=line, capture_output=True, text=True,
            timeout=_TIMEOUT, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return {"ok": False, "error": f"lse-engine exit {proc.returncode}: {proc.stderr[:200]}"}
        out = (proc.stdout or "").strip().splitlines()
        return json.loads(out[-1]) if out else {"ok": False, "error": "empty output"}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def engine_available() -> bool:
    return _engine_path() is not None


def delta_update_lesson(lesson_id: str, delta: float, threshold: float = 0.1) -> dict:
    return _call("delta_update", {"kind": "lesson", "id": lesson_id, "delta": delta, "threshold": threshold})


def lesson_recall(lesson_id: str) -> dict:
    """查询单条教训（不触发 recall_count++，防查询污染枢纽信号）。"""
    return _call("lesson_recall", {"id": lesson_id})


def delta_update_rule(rule_id: str, delta: float, adopted: bool = True) -> dict:
    return _call("delta_update", {"kind": "rule", "id": rule_id, "delta": delta, "adopted": adopted})


def ucb_select(parent: str, children: list, c: float = 1.41) -> dict:
    return _call("ucb_select", {"parent": parent, "children": children, "c": c})


def ucb_backprop(node_id: str, reward: float) -> dict:
    return _call("ucb_backprop", {"id": node_id, "reward": reward})


def experience_store(model: str, ctx: str, delta: float, summary: str) -> dict:
    return _call("experience_store", {"model": model, "ctx": ctx, "delta": delta, "summary": summary})


def experience_match(ctx: str, limit: int = 5) -> dict:
    return _call("experience_match", {"ctx": ctx, "limit": limit})


def state_get() -> dict:
    # TTL 缓存（2026-08-14 高压优化）：cProfile 热点——state_get 每次 spawn
    # lse-engine 子进程（~5.8ms/次，std_check 的 _summarize 高频调用）——
    # 引擎文件未变且 <5s 直接用缓存，spawn 降为 ~0。
    _now = time.time()
    _cached = _STATE_CACHE
    if _cached and _now - _cached[0] < 5.0:
        try:
            p = _engine_path()
            if p and _cached[1] == (os.path.getmtime(p), os.path.getsize(p)):
                return _cached[2]
        except OSError:  # 尽力而为（吞错有注释——可追溯）
            pass
    r = _call("state_get", {})
    try:
        p = _engine_path()
        sig = (os.path.getmtime(p), os.path.getsize(p)) if p else None
        _STATE_CACHE[:] = [time.time(), sig, r]
    except OSError:  # 尽力而为（吞错有注释——可追溯）
        pass
    return r


# state_get 缓存（列表占位可变——避免 global 声明）
_STATE_CACHE: list = [0.0, None, {}]


# ── P1c 进化记忆升级（2026-08-12，抄 mem0 自动提取 + Letta 三层）──
# 三层：core(核心教训，长期) / work(工作教训，短期) / archive(归档)
_LSE_TIERS = ("core", "work", "archive")
# 每条教训 ID 前缀分层：core_ / work_ / archive_
_TIER_PREFIX = {"core": "core_", "work": "work_", "archive": "archive_"}


def lesson_store_tiered(tier: str, content: str, delta: float = 0.0,
                        threshold: float = 0.1) -> dict:
    """分层存教训：core/work/archive 三级（Letta 启发）。

    tier: core(核心，长期保留)/work(工作，短期)/archive(归档)
    实际调用 lse-engine delta_update，ID 加分层前缀。
    """
    if tier not in _LSE_TIERS:
        return {"ok": False, "error": f"未知层级: {tier}（可选 {_LSE_TIERS}）"}
    # 内容哈希 → 稳定 ID（同内容汇聚，防重复；枢纽优先的汇聚基础）
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    lesson_id = f"{_TIER_PREFIX[tier]}{h}"
    return delta_update_lesson(lesson_id, delta, threshold)


def auto_extract_lessons(text: str, tier: str = "work",
                         patterns: list[str] | None = None) -> dict:
    """自动教训提取（抄 mem0 自动记忆提取）：从工具结果/对话文本中提取教训。

    零 LLM 规则版：识别文本中的教训模式（"教训/注意/避免/必须/不要/经验"等信号），
    每条约 200 字符，自动入库（分层）。LLM 版可在后续接 kb_query/蒸馏模型。

    text:     源文本（工具结果/错误信息/对话）
    tier:     默认层级 work（短期），核心教训可显式传 core
    patterns: 自定义教训信号词（默认内置中英信号词）
    """
    if not text or not text.strip():
        return {"ok": False, "error": "text 为空"}
    default_signals = [
        "教训", "注意", "避免", "必须", "不要", "切记", "经验",
        "lesson", "avoid", "never", "always", "remember", "pitfall",
        "错误", "失败", "问题", "bug",
    ]
    signals = patterns or default_signals
    # 统一换行符后按行切分（跳过空/过短行）
    sentences = [s.strip() for s in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if len(s.strip()) >= 8]
    extracted = []
    # IDE 增强 291：教训语言标记（从源文本 file 路径后缀推断——
    # 多语言项目的教训可回溯语言来源）
    _lang = ""
    _m = re.search(r"([A-Za-z_][\w./\\-]*\.(?:rs|py|go|ts|tsx|js|jsx|gd|c|cpp|"
                   r"h|hpp|cs|lua|sh|bash|java|kt|kts|swift|php|rb|ps1|dart))",
                   text)
    if _m:
        _lang = os.path.splitext(_m.group(1))[1].lower().lstrip(".")
    for sent in sentences:
        if any(sig in sent for sig in signals):
            # 截断 200 字符（压缩学习：防状态文件膨胀）
            summary = sent if len(sent) <= 200 else sent[:197] + "..."
            if _lang:
                summary = f"[{_lang}] {summary}"  # 语言前缀——recall 一眼可溯
            r = lesson_store_tiered(tier, summary)
            if r.get("ok", False) and r.get("result", {}).get("ok", True):
                extracted.append(summary)
    return {"ok": True, "tier": tier, "extracted": len(extracted),
            "lessons": extracted[:20], "language": _lang,
            "note": "规则版自动提取（信号词匹配）；LLM 版可接蒸馏模型升级"}