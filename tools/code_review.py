# -*- coding: utf-8 -*-
"""tools/code_review.py —— 多维代码评审域（code_review 单工具）。

S44：多透镜代码评审（找问题不再单一）+ diff 模式（只报改动）。
S65：lens 过滤 + 测试区复杂度豁免；S49：卫生透镜（重复文件/无测试源文件）。
S127：自 tools/scan.py **整体平移**拆出（CONSOLIDATION §三 P0 上帝对象拆分——
壳域 ∥ 评审域双域一文件 650 行、30 天 30 提交全仓最高）。平移不改逻辑：
注册名不变、透镜语义不变、`_iter_files` 改走 tools/filewalk 单一实现；
在 scan.py 时期即为死常量的 `_RE_FUNC_START` 未随迁（零引用，S127 记录）。
"""
import os
import re
import subprocess
from collections import Counter, defaultdict

from tools.fs import _resolve as _fs_resolve
from tools.filewalk import iter_code_files, SCAN_SKIP_DIRS as _SKIP_DIRS

import registry  # S55 同类修复：code_review 的 bug_scan 透镜用 registry.call 却没导入，
                 # NameError 被 except 吞掉——S44 起该透镜从未真正运行过
from registry import tool

_RE_SECRET = re.compile(
    r"(?i)(password|passwd|api_?key|secret|token|access_key)\s*[=:]\s*[\"'][^\"']{6,}")
_RE_DANGER = [
    (re.compile(r"\beval\s*\("), "eval 动态执行"),
    (re.compile(r"\bexec\s*\("), "exec 动态执行"),
    (re.compile(r"\bos\.system\s*\("), "os.system shell 调用"),
    (re.compile(r"subprocess\.[a-z_]+\([^)]*shell\s*=\s*True"), "subprocess shell=True"),
    (re.compile(r"\.innerHTML\s*="), "innerHTML 直接赋值（XSS 面）"),
    (re.compile(r"execute\s*\([^)]*[%+]"), "SQL 拼接执行"),
]
_RE_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_FUNC_LONG = 80
# 24 空格（6 层）对 try/except 密集的基建代码是常规密度；28（7 层）才是真离群
_NEST_SPACES = 28
_PARAMS_MAX = 6


def _lang_of_file(fp):
    ext = os.path.splitext(fp)[1].lower().lstrip(".")
    return {"py": "python", "rs": "rust", "go": "go", "js": "javascript",
            "ts": "javascript", "jsx": "javascript", "tsx": "javascript"}.get(ext)


def _symbol_spans(lines, lang):
    """[(name, start_idx, end_idx, kind)]——顶层+嵌套符号清单（S70）。

    kind：fn / type（rust struct/enum/trait/impl、python class）。
    end_idx 为 1-based 末行（brace=闭合行；python=body 最后一行）。
    brace 语言（rust/go/js）顶层符号用括号深度回 0 定真尾（S64 沿用）；
    python 用缩进回归定 body 末行；嵌套符号（缩进 def/class）也列出。
    """
    if lang == "python":
        pat = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)|^(\s*)class\s+(\w+)")
    elif lang == "rust":
        pat = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"
                         r"|^(?:pub(?:\([^)]*\))?\s+)?"
                         r"(?:struct|enum|trait|impl)\s+(\w+)")
    elif lang == "go":
        pat = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)|^type\s+(\w+)\s*")
    elif lang == "javascript":
        pat = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
                         r"|^\s*class\s+(\w+)")
    else:
        return []
    brace_lang = lang in ("rust", "go", "javascript")
    starts = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        name = m.group(m.lastindex) if m.lastindex else \
            next((g for g in m.groups() if g), None)
        if not name:
            continue
        kind = "fn"
        if (lang == "python" and line.lstrip().startswith("class")) or \
                (lang != "python" and re.search(r"\b(struct|enum|trait|impl)\s+\w", line)):
            kind = "type"
        starts.append((name, i, kind))
    spans = []
    for j, (name, i, kind) in enumerate(starts):
        end = starts[j + 1][1] if j + 1 < len(starts) else \
            min(len(lines), i + _FUNC_LONG * 4)
        if brace_lang and lines[i][:1] not in (" ", "\t"):
            cap = min(len(lines), i + _FUNC_LONG * 6)
            depth = 0
            opened = False
            for k in range(i, cap):
                depth += lines[k].count("{") - lines[k].count("}")
                if "{" in lines[k]:
                    opened = True
                if opened and depth <= 0:
                    end = k + 1
                    break
        elif lang == "python":
            ind = len(lines[i]) - len(lines[i].lstrip())
            last_body = i
            for k in range(i + 1, len(lines)):
                if lines[k].strip():
                    cur = len(lines[k]) - len(lines[k].lstrip())
                    if cur <= ind:
                        break
                    last_body = k
            end = last_body + 1
        spans.append((name, i, end, kind))
    return spans


def _func_spans(lines, lang):
    """[(name, start_idx, end_idx, params)]——函数跨度与参数数（廉价比 AST 稳）。"""
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
                     line) if lang == "rust" else None
        if lang == "python":
            m = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)", line)
        elif lang == "go":
            m = re.match(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", line)
        elif lang == "javascript":
            m = re.match(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", line)
        if not m:
            continue
        # S66：python 模式组 1 是缩进、组 2 才是名字——取最后一个参与组
        # （此前缩进 def 的"名字"是缩进串，complexity 报名全错）
        name = m.group(m.lastindex) if m.lastindex else \
            next((g for g in m.groups() if g), None)
        if not name:
            continue
        params = line.count(",") + 1 if "(" in line and ")" in line else 0
        starts.append((name, i, params))
    spans = []
    for j, (name, i, params) in enumerate(starts):
        end = starts[j + 1][1] if j + 1 < len(starts) else min(len(lines), i + _FUNC_LONG * 3)
        # S64：brace 语言的真函数尾 = 括号深度回到 0——此前跨度
        # 一律到下一个 fn 起点，中间的 struct/常量/注释把行数吹大
        # （VF3 vehicle_compound_parts 真身 59 行被报 82）
        # 仅顶层 fn 适用；嵌套在 mod/class 里的 fn（行首缩进）走原回退
        if lang in ("rust", "go", "javascript") and lines[i][:1] not in (" ", "\t"):
            cap = min(len(lines), i + _FUNC_LONG * 4)
            depth = 0
            opened = False
            for k in range(i, cap):
                depth += lines[k].count("{") - lines[k].count("}")
                if "{" in lines[k]:
                    opened = True
                if opened and depth <= 0:
                    end = k + 1
                    break
        spans.append((name, i, end, params))
    return spans


def _complexity_findings(lines, lang):
    out = []
    for name, i, end, params in _func_spans(lines, lang):
        span = end - i
        if span > _FUNC_LONG:
            out.append((i + 1, f"函数 {name} 长 {span} 行（>{_FUNC_LONG}）——复杂度热点"))
        if params > _PARAMS_MAX:
            out.append((i + 1, f"函数 {name} 参数 {params} 个（>{_PARAMS_MAX}）"))
        # S44 括号深度感知：多行调用的续行缩进不是逻辑嵌套（假阳性修正）
        depth = 0
        bracket = 0
        for ln in lines[i:end]:
            if bracket == 0:
                stripped = len(ln) - len(ln.lstrip())
                if ln.strip():
                    depth = max(depth, stripped)
            bracket += ln.count("(") + ln.count("[") + ln.count("{") \
                - ln.count(")") - ln.count("]") - ln.count("}")
            if bracket < 0:
                bracket = 0
        if depth >= _NEST_SPACES:
            out.append((i + 1, f"函数 {name} 嵌套深 {depth} 空格（≥{_NEST_SPACES}）"))
    return out


def _test_mod_regions(lines):
    """rust #[cfg(test)] + mod X { 的区间（到列 0 的 '}'）——测试夹具的
    长函数不是产品复杂度（S65：VF3 测试夹具污染热点清单）。"""
    regions = []
    i = 0
    while i < len(lines) - 1:
        if lines[i].strip() == "#[cfg(test)]" and \
                re.match(r"^\s*mod\s+\w+\s*\{", lines[i + 1]):
            for k in range(i + 1, len(lines)):
                if lines[k].rstrip() == "}":
                    regions.append((i, k))
                    i = k
                    break
        i += 1
    return regions


def _review_file(fp, changed=None):
    """单文件全透镜。changed=改动行区间列表时只报区间内的发现。"""
    lang = _lang_of_file(fp)
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    lines = src.split("\n")

    def in_changed(ln):
        return changed is None or any(a <= ln <= b for a, b in changed)

    out = []
    for i, line in enumerate(lines, 1):
        if not in_changed(i):
            continue
        m = _RE_SECRET.search(line)
        if m:
            out.append({"lens": "security", "severity": "high", "file": fp,
                        "line": i, "msg": f"疑似硬编码凭据: {m.group(1)}"})
            continue
        for pat, msg in _RE_DANGER:
            if pat.search(line):
                out.append({"lens": "security", "severity": "high", "file": fp,
                            "line": i, "msg": msg})
                break
        m = _RE_TODO.search(line)
        if m:
            out.append({"lens": "todo", "severity": "info", "file": fp,
                        "line": i, "msg": f"{m.group(1)} 标记"})
    if lang:
        # S65：复杂度透镜跳过测试区——rust #[cfg(test)] mod 与 python 测试/
        # conftest 文件；测试夹具的长函数（def/mount 表构造）不是产品复杂度
        stem = os.path.splitext(os.path.basename(fp))[0]
        is_py_test = lang == "python" and (
            stem.startswith("test_") or stem.endswith("_test") or stem == "conftest")
        skip_regions = _test_mod_regions(lines) if lang == "rust" else []
        for ln, msg in _complexity_findings(lines, lang):
            if is_py_test or any(a <= ln <= b for a, b in skip_regions):
                continue
            if in_changed(ln):
                out.append({"lens": "complexity", "severity": "low", "file": fp,
                            "line": ln, "msg": msg})
    return out


def _dup_file_findings(root):
    """S49 卫生透镜：内容完全相同的重复文件（md5 分组，≥2 成员即报）。
    抓 data/ vs dist/data 式构建残留副本——**全文件类型**（.ron/.json 等数据
    文件正是高发区，不能只看代码文件）。"""
    import hashlib
    groups = defaultdict(list)
    # S49 修正：dist/build 不能跳——构建残留副本正是高发区（用户实测：
    # data/modules.ron 与 dist/data/modules.ron 字节级相同）
    keep_skip = ('.git', 'node_modules', '__pycache__', '.unified-rx-index')
    for r, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in keep_skip]
        for fn in fs:
            fp = os.path.join(r, fn)
            try:
                if os.path.getsize(fp) > 1024 * 1024:
                    continue
                with open(fp, "rb") as f:
                    h = hashlib.md5(f.read()).hexdigest()
                groups[h].append(fp)
            except OSError:
                continue
    out = []
    for h, members in groups.items():
        if len(members) < 2:
            continue
        out.append({"lens": "duplication", "severity": "med",
                    "file": members[0], "line": 0,
                    "msg": "重复文件 ×{}: {}".format(
                        len(members), ", ".join(members[1:])[:160])})
    return out


_TEST_LANG_EXTS = (".py", ".java", ".go")


def test_candidates(stem, ext):
    """S129：测试文件候选名（覆盖透镜与 ide_risk_rank 共用的唯一口径）。"""
    return (f"test_{stem}{ext}", f"{stem}_test{ext}",
            f"test_{stem.replace('test_', '')}{ext}")


def _untested_findings(root, files):
    """S49 卫生透镜：有测试约定的语言（py/java/go）源文件无对应测试文件。
    rust 走内联 #[cfg(test)]，文件级约定不适用 → 如实排除。"""
    repo_files = set()
    for r, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            repo_files.add(fn.lower())
    out, seen = [], set()
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        if ext not in _TEST_LANG_EXTS:
            continue
        stem = os.path.splitext(os.path.basename(fp))[0]
        if stem.startswith("test_") or stem.endswith("_test") or stem == "conftest":
            continue
        if any(c in repo_files for c in test_candidates(stem, ext)):
            continue
        if stem in seen:
            continue
        seen.add(stem)
        out.append({"lens": "coverage", "severity": "med", "file": fp,
                    "line": 0, "msg": f"源文件 {stem} 无对应测试文件"})
    return out[:30]


def _git_changed_ranges(repo, base="HEAD"):
    """git diff <base> 的改动行区间 {abspath: [(start,end)]}。base=HEAD（默认）/分支名。含未跟踪文件。"""
    changed = {}
    untracked = []
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "-U0", "--no-color", base],
                           capture_output=True, timeout=120)
        out = (r.stdout or b"").decode(errors="replace")
        cur = None
        for line in out.splitlines():
            m = re.match(r"^\+\+\+ b/(\S+)", line)
            if m:
                cur = os.path.abspath(os.path.join(repo, m.group(1)))
                continue
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m and cur:
                start = int(m.group(1))
                n = int(m.group(2) or 1)
                if n:
                    changed.setdefault(cur, []).append((start, start + n - 1))
        r2 = subprocess.run(["git", "-C", repo, "ls-files", "--others",
                             "--exclude-standard"], capture_output=True, timeout=60)
        for f in (r2.stdout or b"").decode(errors="replace").splitlines():
            if f.strip():
                untracked.append(os.path.abspath(os.path.join(repo, f.strip())))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return changed, untracked


@tool("code_review", "多维代码评审：bug 模式 + 安全（硬编码凭据/危险调用）+ 复杂度"
      "热点 + TODO；mode=diff 只报改动行（评审补丁）；lens 只留指定透镜", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件/目录/git 仓库根"},
           "mode": {"type": "string", "enum": ["file", "diff"],
                    "description": "diff=只评审 git 改动行（默认 file）"},
           "base": {"type": "string",
                    "description": "diff 基线（默认 HEAD；可传分支名评审整个 branch）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 60）"},
           "lens": {"type": "string",
                    "description": "只保留指定透镜（bug_scan/security/complexity/"
                                   "todo/duplication/coverage）——大仓评审免截断"},
       },
       "required": ["path"]})
def code_review(path, mode="file", max_files=60, base="HEAD", lens=None):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if os.path.isfile(path):
        files, changed, untracked = [os.path.abspath(path)], None, []
    elif os.path.isdir(path):
        # S127：遍历改走 tools/filewalk 单一实现（原 scan._iter_files 同语义）
        files = [os.path.abspath(p) for p in iter_code_files(path, max_files)]
        if mode == "diff" and os.path.isdir(os.path.join(path, ".git")):
            changed, untracked = _git_changed_ranges(path, base)
        else:
            changed, untracked = None, []
    else:
        return {"error": f"路径不存在: {path}"}

    findings = []
    for fp in files:
        ch = changed.get(fp) if changed is not None else None
        if changed is not None and ch is None and fp not in untracked:
            continue                     # diff 模式：只评审改动/新增文件
        fch = changed.get(fp) if changed else None
        findings.extend(_review_file(fp, fch))
    # bug_scan 透镜（真扫描器复用）
    target = files[0] if len(files) == 1 else path
    try:
        r = registry.call("bug_scan", {"path": target, "max_files": max_files})
        res = r.get("result") or {}
        for d in res.get("issues") or []:
            fp = os.path.abspath(d["file"])
            ch = changed.get(fp) if changed is not None else None
            if changed is not None and (ch is None or not any(
                    a <= d["line"] <= b for a, b in ch)):
                continue
            findings.append({"lens": "bug_scan", "severity":
                             "high" if d.get("kind") == "definite" else "med",
                             "file": fp, "line": d["line"], "msg": d["msg"][:160]})
    except Exception as e:
        # S72：静默吞异常违反 workflow.md 自己立的规矩——至少留一条协议日志
        registry.notify("warning",
                        f"code_review 的 bug_scan 透镜失败（其余透镜照常）: {type(e).__name__}: {e}")
    # S49 卫生透镜：重复文件 + 无测试源文件（仅目录模式；diff 评审改动不掺卫生面）
    if mode == "file" and os.path.isdir(path):
        findings.extend(_dup_file_findings(path))
        findings.extend(_untested_findings(path, files))
    # S65：lens 过滤——大仓评审免截断（findings 出口有 200 项钳制，
    # 全透镜 468 项时 duplication/coverage 会被挤出）
    if lens:
        findings = [f for f in findings if f["lens"] == lens]
    by_lens = Counter(f["lens"] for f in findings)
    hot = Counter(f["file"] for f in findings if f["severity"] in ("high", "med"))
    if not hot and findings:
        # S65：lens 过滤后可能全是 low/info（如 complexity）——回退按文件计数
        hot = Counter(f["file"] for f in findings)
    return {"mode": mode, "files": len(files), "total": len(findings),
            "by_lens": dict(by_lens), "lens": lens or None,
            "top_hotspots": [{"file": f, "findings": n} for f, n in
                             hot.most_common(5)],
            "findings": sorted(findings, key=lambda x: (
                {"high": 0, "med": 1, "low": 2, "info": 3}[x["severity"]],
                x["file"], x["line"]))[:200]}
