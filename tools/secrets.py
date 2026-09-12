# -*- coding: utf-8 -*-
"""tools/secrets.py —— 凭据/密钥泄漏扫描（S123）：secrets_hunt。

动机：仓库是泄漏重灾区——真 key 提交过一次就永远在 git 历史里。本工具对
目录做**模式匹配 + 高熵启发**两层扫描，输出只给**掩码**（前 4 后 2 + 长度），
完整值必须人工打开文件复核——扫描结果本身不能变成二次泄漏源。

两层口径：
- 模式层（高置信）：AWS AKIA / GitHub ghp_gho_ / Slack xox / Google AIza /
  Stripe sk_live / PEM 私钥头 / JWT 形状 / 通用 secret 赋值（password=、
  api_key: 等，值过滤占位符 changeme/${}/<your >/example）；
- 熵层（嫌疑）：长度 ≥20 的 token，Shannon 熵 ≥ min_entropy（默认 4.5
  bits/char）且至少 3 类字符——只报 suspect，不冒充确认。

诚实边界：
- 这是**静态启发，不是保证**——自定义格式密钥（不匹配任何模式、熵不够）
  漏报；占位符过滤可能放过伪装成占位符的真 key；误报也会有（随机 UUID、
  测试夹具里的假 token）——所有 hit 都要人工复核；
- **Rust 内核加速是后续轮次候选**：性能基线纪律（先测原生 Python 基线，
  Rust 版实测赢了才进 auto），本轮纯 stdlib；
- 扫描结果不外发、不落盘原始值——掩码是产品行为不是可选项。
"""
import math
import os
import re
import time

from registry import tool

DEFAULT_INCLUDE = ("py,rs,js,ts,jsx,tsx,json,yaml,yml,toml,md,go,java,c,h,cpp,"
                   "cs,php,rb,sh,ps1,bat,sql,env,cfg,ini,txt,xml,html,swift,kt")

# 锁文件/构建产物全是高熵哈希，熵层整文件跳过（模式层照样扫）
_ENTROPY_SKIP_NAMES = re.compile(
    r"(^|[._-])(lock|package-lock|poetry\.lock)|(\.min\.js$)|(\.sum$)"
    r"|(go\.sum$)|(\.lock$)", re.IGNORECASE)

# (规则名, 严重级, 编译好的正则)——无嵌套量词，防 ReDoS
_RULES = [
    ("aws_access_key", "high",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", "high",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,250}\b")),
    ("slack_token", "high",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,250}\b")),
    ("google_api_key", "high",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_live_key", "high",
     re.compile(r"\bsk_live_[0-9a-zA-Z]{24,250}\b")),
    ("private_key_block", "critical",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", "medium",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,250}\.[A-Za-z0-9_-]{10,250}"
                r"\.[A-Za-z0-9_-]{5,250}\b")),
    ("secret_assignment", "medium",
     re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|"
                r"access[_-]?key|private[_-]?key|auth[_-]?token|"
                r"client[_-]?secret|secret[_-]?key)\b\s*[:=]\s*"
                r"[\"']?([^\s\"']{12,200})")),
]

# 赋值层占位符过滤：值像这些就不是真密钥（小写包含即滤）
_PLACEHOLDERS = ("changeme", "change-me", "example", "placeholder", "your-",
                 "your_", "<your", "${", "$(", "{%", "{{", "xxx", "todo",
                 "dummy", "n/a", "insert", "replace", "sample", "dummy_",
                 "123456", "qwerty", "letmein", "password", "username",
                 "test123", "abcd", "default")

_TOKEN_RE = re.compile(r"[A-Za-z0-9_/+=-]{20,300}")
_MAX_LINE_SNIPPET = 200
_MAX_HITS_DEFAULT = 200


def _shannon(data):
    if not data:
        return 0.0
    freq = {}
    for ch in data:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _char_classes(tok):
    return sum(bool(cls_re.search(tok)) for cls_re in
               (re.compile(r"[a-z]"), re.compile(r"[A-Z]"),
                re.compile(r"\d"), re.compile(r"[_/+=-]")))


def _mask(tok):
    if len(tok) < 8:
        return "***"
    return f"{tok[:4]}…{tok[-2:]}(len={len(tok)})"


def _is_placeholder(value):
    low = value.lower()
    return any(p in low for p in _PLACEHOLDERS)


def _ext_of(fn):
    base = os.path.basename(fn)
    if base == ".env":
        return "env"
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _walk(root, include_exts, max_files):
    from tools.ide_common import _SKIP_DIRS
    skip = set(_SKIP_DIRS) | {"venv", ".venv", "site-packages", ".tox",
                              "dist", "out", ".idea", ".vscode"}
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if _ext_of(fn) not in include_exts:
                continue
            if count >= max_files:
                return
            count += 1
            yield os.path.join(r, fn)


@tool("secrets_hunt",
      "凭据/密钥泄漏扫描：AWS/GitHub/Slack/Google/Stripe/PEM 私钥/JWT 模式匹配 "
      "+ 高熵 token 嫌疑（Shannon ≥4.5）。输出一律掩码（前4后2+长度），完整值"
      "人工打开文件复核；结果不外发。静态启发非保证——自定义格式漏报、测试夹具"
      "误报，所有 hit 人工确认", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "扫描根目录（默认当前目录）"},
           "include": {"type": "string",
                       "description": "逗号分隔的扩展名白名单（默认常见源码/配置 30+ 种）"},
           "max_file_kb": {"type": "integer", "description": "单文件上限 KB（默认 512）"},
           "min_entropy": {"type": "number",
                           "description": "熵层阈值 bits/char（默认 4.5）"},
           "max_files": {"type": "integer", "description": "最多扫描文件数（默认 3000）"},
           "max_results": {"type": "integer", "description": "返回 hit 上限（默认 200）"}},
       "required": []})
def secrets_hunt(path=None, include=None, max_file_kb=512, min_entropy=4.5,
                 max_files=3000, max_results=_MAX_HITS_DEFAULT):
    t0 = time.time()
    root = os.path.abspath(path or os.getcwd())
    if not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    exts = {e.strip().lstrip(".").lower()
            for e in (include or DEFAULT_INCLUDE).split(",") if e.strip()}
    size_cap = int(max_file_kb) * 1024
    hits, files_scanned, files_skipped = [], 0, 0

    for fp in _walk(root, exts, int(max_files)):
        base = os.path.basename(fp)
        try:
            if os.path.getsize(fp) > size_cap:
                files_skipped += 1
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
                if "\x00" in head:               # 二进制
                    files_skipped += 1
                    continue
                f.seek(0)
                text = f.read()
        except OSError:
            files_skipped += 1
            continue
        files_scanned += 1
        rel = os.path.relpath(fp, root)
        entropy_layer = not _ENTROPY_SKIP_NAMES.search(base)

        for lineno, line in enumerate(text.splitlines(), 1):
            for rule, severity, rx in _RULES:
                for m in rx.finditer(line):
                    value = m.group(m.lastindex) if m.groups() else m.group(0)
                    if rule == "secret_assignment" and _is_placeholder(value):
                        continue
                    snippet = line.strip().replace(value, _mask(value))
                    hits.append({"file": rel, "line": lineno, "rule": rule,
                                 "severity": severity, "masked": _mask(value),
                                 "snippet": snippet[:_MAX_LINE_SNIPPET]})
            if not entropy_layer:
                continue
            for tok in _TOKEN_RE.finditer(line):
                tok_s = tok.group(0)
                if (len(tok_s) >= 20 and _char_classes(tok_s) >= 3
                        and _shannon(tok_s) >= float(min_entropy)):
                    snippet = line.strip().replace(tok_s, _mask(tok_s))
                    hits.append({"file": rel, "line": lineno, "rule": "high_entropy",
                                 "severity": "suspect", "masked": _mask(tok_s),
                                 "snippet": snippet[:_MAX_LINE_SNIPPET]})

    by_sev = {"critical": 0, "high": 0, "medium": 0, "suspect": 0}
    for h in hits:
        by_sev[h["severity"]] = by_sev.get(h["severity"], 0) + 1
    hits.sort(key=lambda h: ({"critical": 0, "high": 1, "medium": 2,
                              "suspect": 3}[h["severity"]], h["file"], h["line"]))
    truncated = len(hits) > int(max_results)
    return {"root": root, "files_scanned": files_scanned,
            "files_skipped": files_skipped, "total_hits": len(hits),
            "by_severity": by_sev, "hits": hits[:int(max_results)],
            "truncated": truncated,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "note": ("掩码展示；完整值人工打开文件复核。静态启发：漏报可能"
                     "（自定义格式）、误报可能（随机 UUID/测试夹具）。若真有"
                     "泄漏：先吊销轮换 key，再清 git 历史（BFG/filter-repo），"
                     "改密不能撤回已泄漏的凭据")}
