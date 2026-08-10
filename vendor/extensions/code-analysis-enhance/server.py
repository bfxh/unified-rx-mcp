"""MCP server: 代码分析增强 (Code Analysis Enhance) — 超越 TRAE/Aether 的 agent 核心能力。

对标 TRAE ai_agent.dll + AetherStudio 源码（D:\\开发\\aetherstudio-src）的机制，
做成纯静态、零 LLM 费用、零网络依赖的 MCP 工具：

1.  file_dedup_state     — FileReadStateCache 同款：文件未变不重读（TRAE read.rs:3034 语义）
2.  change_impact        — 代码变更影响分析：变更符号 → 调用方/测试影响（静态符号索引）
3.  lesson_recall        — core_memory 同款：错误教训自动召回（WARNINGS.md + antipatterns）
4.  code_context         — Aether gather_context 同款 + 超越：光标处符号级 AST 元数据 → 精确 Prompt
                           （三步骤机制：代码解析→元数据→Prompt→模型）
5.  aether_agent_parse   — Aether Agent 标记协议解析器：模型输出的 <<<<<<< AETHER_FILE / RUN / READ
                           标记 → 解析成文件编辑/终端命令/只读请求（Aether ai_agent.rs 移植）
6.  aether_lang_support  — 语言检测：tree-sitter 支持的语言列表/探测（Aether language.rs 移植）
7.  aether_goto_parse    — file:line:col 定位解析（Aether shared parse_goto 移植）
8.  aether_probe         — Aether Studio 安装探测

运行: python server.py   (stdio transport, 与 config.toml [[plugins]] 一致)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

app = Server("code-analysis-enhance")

# ───────────────────────── 1. FileReadStateCache ─────────────────────────

# 文件状态缓存: path -> {mtime, size, sha256}
_FILE_STATE: dict[str, dict] = {}


def _file_fingerprint(path: Path) -> dict | None:
    try:
        st = path.stat()
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return {"mtime": st.st_mtime_ns, "size": st.st_size, "sha256": h.hexdigest()}
    except OSError:
        return None


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="file_dedup_state",
            description=(
                "检查文件是否自上次读取后未变化（FileReadStateCache 同款，TRAE read.rs:3034 语义）。"
                "若返回 unchanged=true，说明文件内容与上次完全一致，应直接沿用之前的读取结果，"
                "不要重新读取——省 token 且防上下文膨胀。首次读取返回 unchanged=false 并记录状态。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="change_impact",
            description=(
                "代码变更影响分析（超越 TRAE）：给定 git 变更文件列表，用静态符号索引分析"
                "每个变更文件的符号（函数/类/方法）及其可能的调用方/测试影响。"
                "纯静态、零 LLM、零网络。返回 {file, symbols, referenced_by, suggested_tests}。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根路径"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "变更文件列表（相对仓库根）",
                    },
                    "symbol_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：额外符号提取正则（默认内置 Go/Python/TS/Rust/GDScript）",
                    },
                },
                "required": ["repo_path", "changed_files"],
            },
        ),
        types.Tool(
            name="lesson_recall",
            description=(
                "错误教训自动召回（core_memory 同款 + antipatterns 增强）：从教训库（WARNINGS.md 风格）"
                "和反模式库（antipatterns.json）中召回与任务描述相关的历史错误教训，"
                "任务前调用可防复发。返回 {lessons, antipatterns, warnings}。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "任务描述，用于匹配相关教训"},
                    "lessons_dir": {
                        "type": "string",
                        "description": "教训库目录（含 WARNINGS.md / *.md / antipatterns.json），默认自动探测",
                    },
                },
                "required": ["task_description"],
            },
        ),
        types.Tool(
            name="code_context",
            description=(
                "光标处符号级代码上下文组装（Aether Studio gather_context 同款机制 + 超越）："
                "1) 传统引擎先跑——用 AST/正则提取当前文件的结构化元数据（类名/函数签名/导入依赖/行号）；"
                "2) 拼成 Prompt——元数据 + 光标附近代码片段按模板组装成'精确的草稿'；"
                "3) 模型再上场——基于这份精确上下文生成。"
                "对应三步骤机制：代码解析→元数据→Prompt→模型。零 LLM 费用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "代码文件绝对路径"},
                    "cursor_line": {"type": "integer", "description": "光标所在行号（1-based），0=无光标"},
                    "radius": {"type": "integer", "description": "光标附近代码行数半径（默认 30）"},
                    "search_repo": {"type": "string", "description": "可选：仓库根路径，启用光标符号的跨文件引用链搜索"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="aether_agent_parse",
            description=(
                "Aether Agent 标记协议解析器（移植自 Aether ai_agent.rs）：解析模型回复中的 "
                "<<<<<<< AETHER_FILE / ======= AETHER_SEP / >>>>>>> AETHER_END_FILE（文件编辑块）、"
                "<<<<<<< AETHER_RUN（终端命令块）、<<<<<<< AETHER_READ / AETHER_LIST（只读请求）。"
                "行锚定+独特哨兵，模型输出这些标记即可直接操作文件/终端，绕过工具调用协议。"
                "返回 {edits, run_commands, tool_requests}。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "response": {"type": "string", "description": "模型回复文本（含 AETHER 标记）"},
                },
                "required": ["response"],
            },
        ),
        types.Tool(
            name="aether_lang_support",
            description=(
                "语言检测（tree-sitter 能力）：探测文件后缀对应的编程语言是否受支持。"
                "支持 c/cpp/rust/python/js/ts/json/toml/html/markdown/gdscript/go/css/lua。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（按后缀探测语言）"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="aether_goto_parse",
            description=(
                "解析 file:line:col 定位串（Aether parse_goto 同款）：'path:12:5' → "
                "{path, line, column, zero_based}。用于跳转/定位光标。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "goto": {"type": "string", "description": "定位串，如 'src/main.rs:42:7'"},
                },
                "required": ["goto"],
            },
        ),
        types.Tool(
            name="lsp_position_convert",
            description=(
                "byte ↔ line:col 双向转换（Aether FastLineIndex 移植，LSP 标准 UTF-16 码元计数，"
                "二分查找 O(log n)）。byte_to_position: 给 byte_offset 返回 {line, character}；"
                "position_to_byte: 给 {line, character} 返回 byte_offset。用于光标定位/编辑映射。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "文档全文"},
                    "direction": {"type": "string", "description": "byte_to_position 或 position_to_byte", "enum": ["byte_to_position", "position_to_byte"]},
                    "byte_offset": {"type": "integer", "description": "direction=byte_to_position 时的字节偏移"},
                    "line": {"type": "integer", "description": "direction=position_to_byte 时的行（0-based）"},
                    "character": {"type": "integer", "description": "direction=position_to_byte 时的列（UTF-16 码元，0-based）"},
                },
                "required": ["text", "direction"],
            },
        ),
        types.Tool(
            name="lsp_semantic_tokens_decode",
            description=(
                "LSP semantic tokens 解码（Aether SemanticTokensDecoder 移植）："
                "每 5 个 uinteger 描述一个 token [deltaLine, deltaStartChar, length, tokenType, tokenModifiers]，"
                "解码为绝对坐标 token 列表。输入 data 为数组或 JSON 字符串。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {"description": "LSP semantic tokens 数据（uinteger 数组或 JSON 字符串）"},
                },
                "required": ["data"],
            },
        ),
        types.Tool(
            name="lsp_edit_merge",
            description=(
                "相邻编辑合并（Aether IncrementalChangeCalculator::merge_edits 移植，含 H-22 修正）："
                "仅合并真正相邻（next.start == current.end）的编辑，避免合并非相邻编辑时丢失中间文本。"
                "edits 格式: [{range: {start: {line, character}, end: {line, character}}, text}]"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "编辑列表（含 range 和 text）",
                    },
                },
                "required": ["edits"],
            },
        ),
        types.Tool(
            name="aether_model_provider",
            description=(
                "Aether 模型服务商配置探测（aether-ai AiProvider 移植）：返回 deepseek/kimi/custom 的 "
                "base_url/default_model/preset_models。不做 LLM 调用（避免'模型调模型'冲突），"
                "只暴露配置供 RX 直接使用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "deepseek/kimi/custom，空=全部"},
                },
            },
        ),
        types.Tool(
            name="lsp_query",
            description=(
                "LSP 交互查询（Aether default_server_config 同款）：spawn 语言服务器子进程，"
                "提供 completion/hover/definition/references。支持 rust(rust-analyzer 已装)/"
                "python(pylsp)/typescript/javascript(typescript-language-server)/c/cpp(clangd)。"
                "文档文本与请求在单次调用内完成，进程用完即关（无状态，不占用常驻资源）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "language_id": {"type": "string", "description": "rust/python/typescript/javascript/c/cpp"},
                    "request": {"type": "string", "description": "completion/hover/definition/references", "enum": ["completion", "hover", "definition", "references"]},
                    "path": {"type": "string", "description": "文档路径"},
                    "line": {"type": "integer", "description": "光标行（0-based）"},
                    "character": {"type": "integer", "description": "光标列（UTF-16 码元，0-based）"},
                    "text": {"type": "string", "description": "文档全文"},
                    "root": {"type": "string", "description": "工作区根目录"},
                },
                "required": ["language_id", "request", "path", "line", "character", "text"],
            },
        ),
        types.Tool(
            name="aether_probe",
            description=(
                "Aether Studio 探测：检查 D:\\开发\\RJ\\IDE\\Aether Studio\\aether-app.exe 是否存在、"
                "是否为可调用目标，并返回探测结果。预留 socket/http 调用位（当前为编译二进制，"
                "如未来提供接口在此扩展）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "aether_path": {
                        "type": "string",
                        "description": "Aether 安装路径，默认 D:\\开发\\RJ\\IDE\\Aether Studio",
                    },
                },
            },
        ),
    ]


# ───────────────────────── 2. 工具实现 ─────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "file_dedup_state":
        return _tool_file_dedup(arguments)
    if name == "change_impact":
        return _tool_change_impact(arguments)
    if name == "lesson_recall":
        return _tool_lesson_recall(arguments)
    if name == "code_context":
        return _tool_code_context(arguments)
    if name == "aether_agent_parse":
        return _tool_aether_agent_parse(arguments)
    if name == "aether_lang_support":
        return _tool_aether_lang_support(arguments)
    if name == "aether_goto_parse":
        return _tool_aether_goto_parse(arguments)
    if name == "lsp_position_convert":
        return _tool_lsp_position_convert(arguments)
    if name == "lsp_semantic_tokens_decode":
        return _tool_lsp_semantic_tokens_decode(arguments)
    if name == "lsp_edit_merge":
        return _tool_lsp_edit_merge(arguments)
    if name == "aether_model_provider":
        return _tool_aether_model_provider(arguments)
    if name == "lsp_query":
        return _tool_lsp_query(arguments)
    if name == "aether_probe":
        return _tool_aether_probe(arguments)
    raise ValueError(f"Unknown tool: {name}")


def _tool_file_dedup(arguments: dict) -> list[types.TextContent]:
    p = Path(arguments.get("path", ""))
    if not p.is_file():
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"文件不存在: {p}", "unchanged": False}))]
    fp = _file_fingerprint(p)
    prev = _FILE_STATE.get(str(p))
    unchanged = bool(prev) and prev == fp
    _FILE_STATE[str(p)] = fp
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "path": str(p),
            "unchanged": unchanged,
            "advice": ("文件未变化，直接沿用之前读取内容，不要重新读取" if unchanged
                       else "文件已变化或首次读取，需要读取内容"),
            "mtime_ns": fp["mtime"],
            "size": fp["size"],
            "sha256": fp["sha256"][:16],
        }, ensure_ascii=False, indent=2))]


# ── 符号提取：内置多语言正则 ──

_SYMBOL_PATTERNS = {
    ".go":   [r"^func\s+([A-Za-z_]\w*)\s*\(", r"^func\s+\([^)]*\)\s+([A-Za-z_]\w*)\s*\("],
    ".py":   [r"^\s*def\s+([A-Za-z_]\w*)\s*\(", r"^\s*class\s+([A-Za-z_]\w*)"],
    ".ts":   [r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)"],
    ".js":   [r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)"],
    ".rs":   [r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)\s*\(", r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)"],
    ".gd":   [r"^\s*func\s+([A-Za-z_]\w*)\s*\(", r"^\s*(?:class_name\s+|extends\s+)([A-Za-z_]\w*)"],
    ".cpp":  [r"^\s*[A-Za-z_:<>,\*\&]+\s+([A-Za-z_]\w*)\s*\(", r"^\s*class\s+([A-Za-z_]\w*)"],
    ".c":    [r"^\s*[A-Za-z_:<>,\*\&]+\s+([A-Za-z_]\w*)\s*\("],
    ".java": [r"^\s*(?:public|private|protected|static|final|abstract|synchronized|\s)*[A-Za-z_<>\?,\[\]]+\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:public|abstract|final)\s+class\s+([A-Za-z_]\w*)",
              r"^\s*(?:public|abstract)\s+interface\s+([A-Za-z_]\w*)"],
    ".cs":   [r"^\s*(?:public|private|protected|internal|static|virtual|override|async|\s)*[A-Za-z_<>\?,\[\]]+\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:public|abstract|sealed|static|partial)\s+class\s+([A-Za-z_]\w*)",
              r"^\s*(?:public)\s+interface\s+([A-Za-z_]\w*)"],
    ".kt":   [r"^\s*(?:public|private|protected|internal|suspend|inline|tailrec|operator|override|fun\s)*fun\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:public|private|data|sealed|open|abstract)\s+(?:class|interface|object)\s+([A-Za-z_]\w*)"],
    ".swift":[r"^\s*(?:public|private|internal|fileprivate|static|final|override|func\s)*func\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:public|private|internal|final|open|struct|class|enum|protocol)\s+(?:class|struct|enum|protocol)\s+([A-Za-z_]\w*)"],
    ".php":  [r"^\s*(?:public|private|protected|static|final|abstract|function\s)*function\s+([A-Za-z_]\w*)\s*\(",
              r"^\s*(?:abstract|final)\s+class\s+([A-Za-z_]\w*)",
              r"^\s*interface\s+([A-Za-z_]\w*)"],
    ".rb":   [r"^\s*(?:def|def self)\.?\s*([A-Za-z_]\w*)\s*\(",
              r"^\s*class\s+([A-Za-z_]\w*)"],
    ".lua":  [r"^\s*(?:local\s+)?function\s+([A-Za-z_]\w*)\s*\("],
    ".sh":   [r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{", r"^\s*function\s+([A-Za-z_]\w*)\s*\("],
}


_REDOS_STRUCTURE_PATTERNS = [
    # 嵌套/重复量词结构（易指数回溯）：(a+)+  (a|b)*  (a?)*  (a{1,})+  (a+(b))+  a*a*  [a]+[a]*  (ab){2,9} 等
    re.compile(r"\(\s*[^)]*[+*?{][^)]*\)\s*[+*?{]"),  # (…+…)+  (…*…)*  (…?…)*  (…{m,})+
    re.compile(r"\(\s*[^)]*\|[^)]*\)\s*[+*{]"),        # (a|b)+  (a|b)*  (a|b){n}
    re.compile(r"\[[^\]]*\]\s*[+*?]\s*\["),            # [a]+[a]*  [a]?[b]+
    re.compile(r"[^*+?{}]\*[^*]+\*"),                  # a*b* 形式双星
    re.compile(r"\([^)]*\)\{[2-9]\d*,[2-9]\d*\}"),     # (…){m,n} 大范围
    re.compile(r"\([^()]*\([^()]*\)[^()]*\)\s*[+*?{]"),  # 组内组+外层量词 (a+(b))+
    re.compile(r"[+*?][A-Za-z0-9_\[\]\\][+*?][A-Za-z0-9_\[\]\\]*[+*?]"),  # 三明治 a+b*a+（O(n²) 回溯）
    re.compile(r"[+*?](?:\[[^\]]*\]|\\[A-Za-z])[+*?](?:\[[^\]]*\]|\\[A-Za-z])[+*?]"),  # 三明治含字符集/转义类 [a-z]+\d+[a-z]+
    re.compile(r"(?:\\[A-Za-z]|[A-Za-z0-9_]|\])\{[1-9]\d{0,2}(?:,[1-9]\d{0,2})?\}\s*(?:\\[A-Za-z]|[A-Za-z0-9_]|\])\{[1-9]\d{0,2}(?:,[1-9]\d{0,2})?\}"),  # 有界量词链 a{1,100}a{1,100}（O(n²)）
    # 任意 {} 量词（含 {m,}/{0,n}/{m}）间隔 ≤8 字符连续：a{1,}a{1,}b、a{1,}aaaaaa{1,}b
    # （O(n²~n³)，security sa_20260809_003600/sa_20260809_010648）
    re.compile(r"\{[0-9]*(?:,[0-9]*)?\}[^+*?{}\[\]]{0,8}\{[0-9]*(?:,[0-9]*)?\}"),
    # 通用两量词重叠链：量词→可重叠字符→量词（a+a+b、a+a{1,}b、a+[a]+b、a+\w+b、\d+\d+b）
    # （security sa_20260809_004631：+/*/? 与 {} 混合的两量词形态全漏网，O(n²)）
    re.compile(r"[+*?](?:(?:\\[A-Za-z]|[A-Za-z0-9_])|\[[^\]]*\])(?:\{[0-9]*(?:,[0-9]*)?\}|[+*?])"),
]

_SYMBOL_CONTENT_LIMIT = 1_000_000  # 符号提取 content 上限（防二次回溯 DoS，security sa_20260808_234752）


def _read_text_limited(path: Path) -> str:
    """读文件限 1MB：先 stat 前置拒绝大文件（防 GB 级先读后截断，security sa_20260809_004631）。"""
    try:
        if path.stat().st_size > _SYMBOL_CONTENT_LIMIT * 2:
            # 超过 2MB 直接拒绝（UTF-8 多字节时 1MB 字符可能 >1MB 字节）
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:_SYMBOL_CONTENT_LIMIT]
    except OSError:
        return ""


def _is_redos_risky(pat: str) -> bool:
    """结构黑名单：拒绝易 ReDoS 的嵌套/多重量词结构（security 审查修复）。"""
    if len(pat) > 200:
        return True
    return any(r.search(pat) for r in _REDOS_STRUCTURE_PATTERNS)


def _extract_symbols(content: str, suffix: str, extra: list[str] | None) -> list[str]:
    # content 上限（防二次回溯 DoS，security sa_20260808_234752）
    if len(content) > _SYMBOL_CONTENT_LIMIT:
        content = content[:_SYMBOL_CONTENT_LIMIT]
    pats = list(_SYMBOL_PATTERNS.get(suffix, []))
    if extra:
        # 用户提供的正则受限：最多 5 个、≤200 字符、且拒绝易 ReDoS 结构
        # （security 审查 sa_20260808_233440：长度限制不够，(a+)+$ 仅 5 字符即可指数回溯）
        sanitized = []
        for p in extra[:5]:
            if isinstance(p, str) and 0 < len(p) <= 200 and not _is_redos_risky(p):
                sanitized.append(p)
        pats.extend(sanitized)
    symbols = []
    for pat in pats:
        try:
            for m in re.finditer(pat, content, re.M):
                # 无捕获组正则（如 \w+）用整个匹配，有组用组 1（review sa_20260808_234216 修复）
                symbols.append(m.group(1) if m.groups() else m.group(0))
        except re.error:
            continue
    return sorted(set(symbols))


def _tool_change_impact(arguments: dict) -> list[types.TextContent]:
    repo = Path(arguments.get("repo_path", ""))
    changed = arguments.get("changed_files", [])
    extra = arguments.get("symbol_patterns") or []
    if not repo.is_dir():
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"仓库路径不存在: {repo}"}, ensure_ascii=False))]
    # 路径越界校验 + 数组上限（security sa_20260808_234752）
    if not isinstance(changed, list) or len(changed) > 50:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "changed_files 必须是 ≤50 的数组"}, ensure_ascii=False))]
    repo_resolved = repo.resolve()
    results = []
    for rel in changed:
        f = (repo / rel).resolve()
        try:
            f.relative_to(repo_resolved)
        except ValueError:
            results.append({"file": str(rel), "ok": False, "reason": "路径越界（.. 逃逸被拒绝）"})
            continue
        suffix = f.suffix.lower()
        if not f.is_file():
            results.append({"file": str(rel), "ok": False, "reason": "文件不存在"})
            continue
        try:
            f_size = f.stat().st_size
        except OSError:
            results.append({"file": str(rel), "ok": False, "reason": "文件无法访问"})
            continue
        if f_size > _SYMBOL_CONTENT_LIMIT * 2:
            results.append({"file": str(rel), "ok": False, "reason": "文件超过 2MB 上限"})
            continue
        content = _read_text_limited(f)
        symbols = _extract_symbols(content, suffix, extra)
        # 引用方：在仓库内搜符号名（限制扫描深度，静态）
        referenced_by = []
        if symbols:
            try:
                out = subprocess.run(
                    ["grep", "-rn", "--include=*", "-l", "--"] +
                    [s for s in symbols[:8]],
                    cwd=str(repo), capture_output=True, text=True, timeout=20,
                )
                for line in out.stdout.splitlines()[:20]:
                    if line and not line.endswith(rel):
                        referenced_by.append(line)
            except (subprocess.TimeoutExpired, OSError):
                referenced_by = ["(grep 不可用或超时)"]
        # 建议测试文件（语言特定命名约定）
        stem = f.stem
        suffix = f.suffix.lstrip(".")
        test_conventions = {
            "py":  [f"tests/test_{stem}.py", f"test_{stem}.py", f"tests/{stem}_test.py"],
            "rs":  [f"tests/{stem}_test.rs", f"src/{stem}_test.rs", f"{stem}_test.rs"],
            "go":  [f"{stem}_test.go", f"internal/{stem}/test_{stem}.go"],
            "java": [f"src/test/java/{stem}Test.java", f"test/{stem}Test.java", f"tests/{stem}Test.java"],
            "ts":  [f"tests/{stem}.test.ts", f"__tests__/{stem}.test.ts", f"{stem}.test.ts"],
            "js":  [f"tests/{stem}.test.js", f"__tests__/{stem}.test.js", f"{stem}.test.js"],
            "kt":  [f"src/test/kotlin/{stem}Test.kt", f"test/{stem}Test.kt"],
            "swift": [f"Tests/{stem}Tests.swift", f"{stem}Tests.swift"],
        }
        conv = test_conventions.get(suffix, [f"tests/test_{stem}.{suffix}", f"test_{stem}.{suffix}"])
        suggested = [str(repo / p) for p in conv]
        suggested = [s for s in suggested if Path(s).exists()]
        results.append({
            "file": rel, "ok": True,
            "symbols": symbols,
            "referenced_by": list(dict.fromkeys(referenced_by)),  # 去重保序
            "suggested_tests": suggested,
        })
    return [types.TextContent(type="text", text=json.dumps(
        {"ok": True, "results": results}, ensure_ascii=False, indent=2))]


def _tool_lesson_recall(arguments: dict) -> list[types.TextContent]:
    task = arguments.get("task_description", "")
    lessons_dir = arguments.get("lessons_dir") or ""
    # 教训库候选路径（自动探测）
    candidates = []
    if lessons_dir:
        candidates.append(Path(lessons_dir))
    candidates += [
        Path(r"D:\AI\Claude\CLAUSE\python\WARNINGS.md"),
        Path(r"D:\AI\Claude\CLAUSE\python\storage\Brain\memory\antipatterns.json"),
        Path(r"D:\开发\泰拉科技\docs"),
        Path(r"D:\开发\reasonix-src\WARNINGS.md"),
        Path(r"D:\开发\reasonix-src\docs"),
    ]
    # 扩展探测：明确目录下的 WARNINGS.md / antipatterns.json / lessons.md
    # （os.walk 限深 3 层 + 结果缓存，避免每次调用全盘慢扫）
    rx_memory = Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\memory")
    _LESSON_CACHE: dict = {}  # {tuple(bases) -> [candidates]}
    _LESSON_CACHE_MAX = 32
    cache_key = tuple(str(b) for b in (Path(r"D:\开发\泰拉科技"), Path(r"D:\开发\reasonix-src"),
                                        Path(r"D:\AI\Claude\CLAUSE\python"), Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace"),
                                        rx_memory))
    if cache_key in _LESSON_CACHE:
        candidates += _LESSON_CACHE[cache_key]
    else:
        _discovered = []
        for base in (Path(r"D:\开发\泰拉科技"), Path(r"D:\开发\reasonix-src"),
                     Path(r"D:\AI\Claude\CLAUSE\python"), Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace"),
                     rx_memory):
            if not base.exists():
                continue
            try:
                for root, dirs, files in os.walk(base):
                    depth = root[len(str(base)):].count(os.sep)
                    if depth > 3:
                        dirs[:] = []
                        continue
                    for fn in files:
                        fl = fn.lower()
                        if fl in ("warnings.md", "antipatterns.json", "lessons.md") or (fn.endswith(".md") and "feedback" in fl):
                            _discovered.append(Path(root) / fn)
                        if len(_discovered) >= 20:
                            break
                    if len(_discovered) >= 20:
                        break
            except (OSError, PermissionError):
                continue
        _LESSON_CACHE[cache_key] = _discovered
        if len(_LESSON_CACHE) > _LESSON_CACHE_MAX:
            _LESSON_CACHE.clear()
        candidates += _discovered
    # RX memory 索引自动注入：MEMORY.md 摘要（如存在）作为全局背景
    rx_index = rx_memory / "global" / "MEMORY.md"
    if rx_index.exists():
        candidates.append(rx_index)
    lessons = []
    antipatterns = []
    warnings = []
    # 关键词提取（任务描述中的中文/英文名词）
    keywords = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z_]{4,}", task)
    keywords = [k.lower() for k in keywords][:12]

    for c in candidates:
        if not c.exists():
            continue
        try:
            if c.suffix == ".json":
                raw = c.read_text(encoding="utf-8")
                if len(raw) > _SYMBOL_CONTENT_LIMIT:
                    raw = raw[:_SYMBOL_CONTENT_LIMIT]  # 限 1MB（security sa_20260809_000110）
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for it in items:
                    pat = str(it.get("pattern", "")).lower()
                    desc = str(it.get("description", "")).lower()
                    if any(k in pat or k in desc for k in keywords):
                        antipatterns.append(it)
            else:
                text = c.read_text(encoding="utf-8", errors="ignore")
                if len(text) > _SYMBOL_CONTENT_LIMIT:
                    text = text[:_SYMBOL_CONTENT_LIMIT]  # 限 1MB（security sa_20260809_000110）
                # 按节切分，粗略匹配含关键词的段落
                for para in re.split(r"\n{2,}", text):
                    if any(k in para.lower() for k in keywords):
                        lessons.append(para.strip()[:400])
                        if len(lessons) >= 8:
                            break
        except (OSError, json.JSONDecodeError):
            continue

    # WARNINGS.md 特有：第 3 节 bug 记忆
    wn = Path(r"D:\AI\Claude\CLAUSE\python\WARNINGS.md")
    if wn.exists():
        text = wn.read_text(encoding="utf-8", errors="ignore")
        if "3. Bug 记忆" in text:
            sec = text.split("## 3. Bug 记忆", 1)[1].split("## ", 1)[0]
            warnings.append(sec.strip()[:800])

    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "task_keywords": keywords,
            "lessons": lessons,
            "antipatterns": antipatterns,
            "warnings": warnings,
            "advice": ("任务前已召回历史教训；如命中反模式，执行时明确避免" if (lessons or antipatterns)
                       else "未命中历史教训，按常规流程执行"),
        }, ensure_ascii=False, indent=2))]


def _tool_aether_probe(arguments: dict) -> list[types.TextContent]:
    base = Path(arguments.get("aether_path") or r"D:\开发\RJ\IDE\Aether Studio")
    exe = base / "aether-app.exe"
    result = {
        "ok": False,
        "aether_path": str(base),
        "exe_exists": exe.exists(),
        "kind": "unknown",
        "note": "",
    }
    if exe.exists():
        result["ok"] = True
        result["kind"] = "rust-native-binary"
        result["note"] = (
            "Aether Studio 是已编译 Rust 原生应用，当前无插件接口/CLI 参数/端口可调用。"
            "预留：若未来提供 socket/http/CLI 接口，在此扩展调用。"
        )
    else:
        result["note"] = "Aether Studio 未在默认路径找到"
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ───────────────────────── 4. code_context（Aether 同款 + 超越）─────────────────────────
# 对应 Aether gather_context 机制，但升级为"光标处符号级"精确元数据：
# 1) 传统引擎先跑：AST/正则提取当前文件的结构化元数据（类名/函数签名/导入依赖）
# 2) 拼成 Prompt：元数据 + 光标附近代码片段 + 诊断 → 精确上下文模板
# 3) 模型再上场：模型基于这份"精确的草稿"生成（用户要求的三步骤机制）

import ast as _ast


def _python_metadata(content: str) -> dict:
    """Python AST 精确解析：类/函数签名/导入依赖。"""
    meta = {"classes": [], "functions": [], "imports": [], "lines": len(content.splitlines())}
    try:
        tree = _ast.parse(content)
    except SyntaxError as e:
        meta["parse_error"] = f"line {e.lineno}: {e.msg}"
        return meta
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef):
            bases = [b.id if isinstance(b, _ast.Name) else "<expr>" for b in node.bases]
            meta["classes"].append({"name": node.name, "line": node.lineno, "bases": bases})
        elif isinstance(node, _ast.FunctionDef):
            args = [a.arg for a in node.args.args]
            ret = None
            if node.returns:
                ret = _ast.unparse(node.returns) if hasattr(_ast, "unparse") else "<ret>"
            meta["functions"].append({
                "name": node.name, "line": node.lineno, "args": args, "returns": ret,
                "is_method": isinstance(getattr(node, "_parent", None), _ast.ClassDef),
            })
        elif isinstance(node, _ast.Import):
            for a in node.names:
                meta["imports"].append(a.name)
        elif isinstance(node, _ast.ImportFrom):
            for a in node.names:
                meta["imports"].append(f"{node.module}.{a.name}" if node.module else a.name)
    # 父级标记（_ast.walk 不保证 parent，改用行号推断方法归属）
    class_lines = [(c["name"], c["line"]) for c in meta["classes"]]
    for fn in meta["functions"]:
        fn["is_method"] = any(c_line < fn["line"] for _, c_line in class_lines)
    return meta


def _generic_metadata(content: str, suffix: str) -> dict:
    """非 Python 语言的增强正则提取。"""
    meta = {"classes": [], "functions": [], "imports": [], "lines": len(content.splitlines())}
    pats = _SYMBOL_PATTERNS.get(suffix, [])
    for pat in pats:
        try:
            for m in re.finditer(pat, content, re.M):
                name = m.group(1)
                line = content[: m.start()].count("\n") + 1
                if pat.endswith("\\s*\\("):  # 函数
                    meta["functions"].append({"name": name, "line": line})
                else:
                    meta["classes"].append({"name": name, "line": line})
        except re.error:
            continue
    # 导入依赖（每个后缀一个正则字符串）
    import_pats = {
        ".go": r"^\s*(?:import|from)\s+[\(]?[\"\w\./\-]",
        ".rs": r"^\s*use\s+[\w:]+",
        ".ts": r"^\s*import\s+.+from\s+['\"][\w@/\-\.]+['\"]",
        ".js": r"^\s*import\s+.+from\s+['\"][\w@/\-\.]+['\"]",
        ".gd": r"^\s*(?:preload|load)\s*\(['\"][\w/\.\-]+['\"]\)",
        ".py": r"^\s*(?:import|from)\s+[\w\.]+",
        ".java": r"^\s*import\s+[\w\.\*]+;",
        ".cs": r"^\s*using\s+[\w\.]+;",
        ".kt": r"^\s*import\s+[\w\.\*]+",
        ".swift": r"^\s*import\s+[\w\.]+",
        ".php": r"^\s*(?:use|require|include)[_\w]*\s+[\w\\]+",
        ".rb": r"^\s*require[\w_]*\s+['\"][\w/\.\-]+['\"]",
        ".lua": r"^\s*require\s*\(?['\"][\w/\.\-]+['\"]\)?",
    }
    ip = import_pats.get(suffix)
    if ip:
        try:
            for m in re.finditer(ip, content, re.M):
                meta["imports"].append(m.group(0).strip())
        except re.error:
            pass
    meta["imports"] = list(dict.fromkeys(meta["imports"]))[:20]
    return meta


def _find_cursor_symbol(meta: dict, cursor_line: int) -> dict | None:
    """找光标所在行的类/函数（光标处符号）。"""
    best = None
    for kind in ("functions", "classes"):
        for s in meta.get(kind, []):
            if s["line"] <= cursor_line:
                if best is None or s["line"] > best["line"]:
                    best = {**s, "kind": kind}
    return best


def _build_prompt(meta: dict, cursor_symbol: dict | None, snippet: str, path: str, lang: str) -> str:
    """拼成'精确的草稿' Prompt（对应 Aether wrap_code_block + build_chat_prompt）。"""
    parts = []
    parts.append(f"// file: {path} (language: {lang}, {meta.get('lines', '?')} 行)")
    if meta.get("imports"):
        parts.append("// 导入依赖: " + ", ".join(meta["imports"][:15]))
    if meta.get("classes"):
        cls_desc = "; ".join(f"{c['name']}(L{c['line']})" for c in meta["classes"][:10])
        parts.append(f"// 类: {cls_desc}")
    if meta.get("functions"):
        fn_desc = "; ".join(f"{f['name']}({', '.join(f.get('args', []))[:60]})(L{f['line']})" for f in meta["functions"][:10])
        parts.append(f"// 函数: {fn_desc}")
    if cursor_symbol:
        kind = cursor_symbol.get("kind")
        parts.append(f"// 【光标处符号】{kind}: {cursor_symbol.get('name')} (L{cursor_symbol.get('line')})")
    if meta.get("parse_error"):
        parts.append(f"// ⚠ 解析警告: {meta['parse_error']}")
    parts.append("// ── 光标附近代码片段 ──")
    parts.append(snippet)
    return "\n".join(parts)


def _tool_code_context(arguments: dict) -> list[types.TextContent]:
    """光标处符号级 AST 元数据 + 附近代码 → 精确 Prompt（传统引擎先跑，模型最后上场）。

    search_repo: 可选，仓库根路径——启用后对光标处符号做跨文件引用搜索，
    返回 {file, line_count} 引用列表（引用链）。
    """
    path = Path(arguments.get("path", ""))
    cursor_line = int(arguments.get("cursor_line", 0))
    radius = int(arguments.get("radius", 30))
    search_repo = arguments.get("search_repo", "")
    if not path.is_file():
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"文件不存在: {path}"}, ensure_ascii=False))]
    content = _read_text_limited(path)
    if not content and path.exists() and path.stat().st_size > _SYMBOL_CONTENT_LIMIT * 2:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"文件超过 2MB 上限: {path}"}, ensure_ascii=False))]
    if len(content) > _SYMBOL_CONTENT_LIMIT:
        content = content[:_SYMBOL_CONTENT_LIMIT]  # 全量读限 1MB（security sa_20260809_000110）
    suffix = path.suffix.lower()
    meta = _python_metadata(content) if suffix == ".py" else _generic_metadata(content, suffix)
    # 光标附近代码片段
    lines = content.splitlines()
    if cursor_line > 0:
        start = max(0, cursor_line - 1 - radius)
        end = min(len(lines), cursor_line - 1 + radius)
        snippet_lines = []
        for i in range(start, end):
            snippet_lines.append(f"{i+1:>4}| {lines[i]}")
        snippet = "\n".join(snippet_lines)
    else:
        snippet = content[:4000]
    cursor_symbol = _find_cursor_symbol(meta, cursor_line) if cursor_line > 0 else None
    # 跨文件引用链（可选）
    references = []
    if search_repo and cursor_symbol:
        sym_name = cursor_symbol.get("name", "")
        repo = Path(search_repo)
        if repo.is_dir() and sym_name:
            try:
                out = subprocess.run(
                    ["grep", "-rn", "--include=*", "-l", sym_name],
                    cwd=str(repo), capture_output=True, text=True, timeout=30,
                )
                for line in out.stdout.splitlines()[:25]:
                    if line and line != str(path):
                        # 统计该文件中的引用行号 + 行数
                        try:
                            f = repo / line
                            hit_lines = []
                            with open(f, encoding="utf-8", errors="ignore") as fh:
                                for i, ln in enumerate(fh, 1):
                                    if i > 10000:
                                        break  # 引用链读文件行数上限（security sa_20260809_003600）
                                    if sym_name in ln:
                                        hit_lines.append(i)
                            references.append({"file": line, "hits": len(hit_lines), "lines": hit_lines[:10]})
                        except OSError:
                            references.append({"file": line, "hits": 0, "lines": []})
            except (subprocess.TimeoutExpired, OSError):
                references = []
    prompt = _build_prompt(meta, cursor_symbol, snippet, str(path), suffix.lstrip(".") or "txt")
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "path": str(path),
            "cursor_line": cursor_line,
            "metadata": meta,
            "cursor_symbol": cursor_symbol,
            "references": references,
            "prompt": prompt,
            "prompt_chars": len(prompt),
            "note": "这是'传统引擎先跑'产出的精确上下文：元数据+附近代码已拼好，模型可直接基于此生成；references 为光标符号的跨文件引用链",
        }, ensure_ascii=False, indent=2))]


# ───────────────────────── 3. 启动 ─────────────────────────

# ───────────────────────── 5. Aether 能力移植 ─────────────────────────
# 移植自 D:\开发\aetherstudio-src：
#   - ai_agent.rs   (Agent 标记协议解析器)
#   - language.rs   (tree-sitter 语言检测)
#   - shared 的 parse_goto (file:line:col 定位)

# Aether Agent 标记协议常量（与 ai_agent.rs 完全一致）
FILE_HEADER_PREFIX = "<<<<<<< AETHER_FILE"
FILE_SEP = "======= AETHER_SEP"
FILE_FOOTER = ">>>>>>> AETHER_END_FILE"
RUN_HEADER = "<<<<<<< AETHER_RUN"
RUN_FOOTER = ">>>>>>> AETHER_END_RUN"
READ_PREFIX = "<<<<<<< AETHER_READ"
LIST_PREFIX = "<<<<<<< AETHER_LIST"

# 支持的语言（tree-sitter get_language / supports_language）
SUPPORTED_LANGS = [
    "c", "cpp", "rust", "python", "javascript", "typescript", "json",
    "toml", "html", "markdown", "gdscript", "go", "css", "lua",
]


def _parse_file_header(line: str) -> str | None:
    """识别 AETHER_FILE 头行，返回路径（可能为空串=新建/整文件替换）。"""
    if not line.startswith(FILE_HEADER_PREFIX):
        return None
    rest = line[len(FILE_HEADER_PREFIX):]
    if rest == "" or rest[0].isspace():
        return rest.strip()
    return None


def _tool_aether_agent_parse(arguments: dict) -> list[types.TextContent]:
    """解析模型回复中的 Agent 标记（Aether ai_agent.rs 同款）：文件编辑/终端命令/只读请求。"""
    response = arguments.get("response", "")
    lines = response.splitlines()
    edits = []
    run_commands = []
    tool_requests = []
    i = 0
    n = len(lines)
    while i < n:
        t = lines[i].rstrip()
        # 文件编辑块
        path = _parse_file_header(t)
        if path is not None:
            i += 1
            search_lines = []
            found_sep = False
            while i < n:
                if lines[i].rstrip() == FILE_SEP:
                    found_sep = True
                    i += 1
                    break
                search_lines.append(lines[i])
                i += 1
            if not found_sep:
                break
            replace_lines = []
            while i < n:
                if lines[i].rstrip() == FILE_FOOTER:
                    i += 1
                    break
                replace_lines.append(lines[i])
                i += 1
            search = "\n".join(search_lines)
            replace = "\n".join(replace_lines)
            edit = {
                "path": path or "(未指定，默认当前文件)",
                "search": search,
                "replace": replace,
                "kind": ("create" if not search.strip() else
                         "delete" if not replace.strip() else "replace"),
            }
            # 路径通配展开：path 含 *?[ 时按 glob 展开为多个编辑
            # 支持排除：行内用 | 分隔，如 "src/*.rs|src/tests/**" 排除后段
            if path and any(ch in path for ch in "*?["):
                import glob as _glob
                include_part = path
                exclude_part = ""
                if "|" in path:
                    include_part, _, exclude_part = path.partition("|")
                matches = sorted(_glob.glob(include_part, recursive=True))
                if exclude_part:
                    excl = set(_glob.glob(exclude_part, recursive=True))
                    matches = [m for m in matches if m not in excl]
                # glob 结果上限（防 C:\** 扫盘爆量，security sa_20260808_234752）
                if len(matches) > 100:
                    matches = matches[:100]
                if matches:
                    for mp in matches:
                        edit_c = dict(edit)
                        edit_c["path"] = mp
                        edit_c["wildcard"] = f"{include_part} → {mp}"
                        edits.append(edit_c)
                else:
                    edit["wildcard"] = f"{include_part} (无匹配)"
                    edits.append(edit)
            else:
                edits.append(edit)
            continue
        # 终端命令块
        if t == RUN_HEADER:
            i += 1
            while i < n:
                if lines[i].rstrip() == RUN_FOOTER:
                    i += 1
                    break
                cmd = lines[i].strip()
                if cmd:
                    run_commands.append(cmd)
                i += 1
            continue
        # 只读请求：READ / LIST（单行指令）
        for prefix in (READ_PREFIX, LIST_PREFIX):
            if t.startswith(prefix):
                rest = t[len(prefix):].strip()
                tool_requests.append({
                    "kind": "read" if prefix == READ_PREFIX else "list",
                    "path": rest or ".",
                })
                break
        i += 1
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "edits": edits,
            "run_commands": run_commands,
            "tool_requests": tool_requests,
            "summary": f"解析出 {len(edits)} 个文件编辑、{len(run_commands)} 条命令、{len(tool_requests)} 个只读请求",
            "note": "协议移植自 Aether ai_agent.rs：行锚定+独特哨兵，模型输出这些标记即可直接操作文件/终端",
        }, ensure_ascii=False, indent=2))]


def _is_retryable_error(resp: dict) -> bool:
    """LSP 重试判定：只有 error.code == -32801（content modified）才重试。
    result:null 是合法空结果，不重试。（review 建议提取为函数供测试引用，避免复制逻辑）"""
    _err = resp.get("error")
    return isinstance(_err, dict) and _err.get("code") == -32801


def _tool_aether_lang_support(arguments: dict) -> list[types.TextContent]:
    """语言检测：探测文件语言是否受支持（tree-sitter 能力）。"""
    path = arguments.get("path", "")
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "py": "python", "rs": "rust", "go": "go", "c": "c", "h": "c",
        "cpp": "cpp", "cc": "cpp", "hpp": "cpp", "js": "javascript", "mjs": "javascript",
        "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
        "json": "json", "toml": "toml", "html": "html", "htm": "html",
        "md": "markdown", "gd": "gdscript", "css": "css", "lua": "lua",
        "java": "java", "cs": "csharp", "kt": "kotlin", "kts": "kotlin",
        "swift": "swift", "php": "php", "rb": "ruby", "sh": "shell", "bash": "shell",
    }
    detected = mapping.get(suffix)
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "path": path,
            "suffix": suffix,
            "detected_language": detected,
            "supported": detected in SUPPORTED_LANGS,
            "supported_languages": SUPPORTED_LANGS,
        }, ensure_ascii=False, indent=2))]


def _tool_aether_goto_parse(arguments: dict) -> list[types.TextContent]:
    """解析 file:line:col 定位串（Aether parse_goto 同款）。"""
    goto = arguments.get("goto", "")
    m = re.match(r"^(.*?)(?::(\d+))?(?::(\d+))?$", goto)
    if not m:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"无法解析: {goto}"}, ensure_ascii=False))]
    path, line_s, col_s = m.groups()
    line = int(line_s) if line_s else 0
    col = int(col_s) if col_s else 0
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "input": goto,
            "path": path.strip(),
            "line": line,
            "column": col,
            "zero_based_line": max(0, line - 1),
            "zero_based_column": max(0, col - 1),
        }, ensure_ascii=False, indent=2))]


# ───────────────────────── 6. Aether LSP 算法移植 ─────────────────────────
# 移植自 D:\开发\aetherstudio-src：
#   - incremental_sync.rs (FastLineIndex / IncrementalChangeCalculator / merge_edits)
#   - semantic_tokens.rs   (SemanticTokensDecoder::decode)


def _build_line_starts(text: str) -> list[int]:
    """FastLineIndex 同款：预计算每行起始字节偏移（注意是字节偏移，非字符索引）。"""
    starts = [0]
    byte_count = 0
    for ch in text:
        byte_count += len(ch.encode("utf-8"))
        if ch == "\n":
            starts.append(byte_count)
    return starts


def _byte_to_position(text: str, line_starts: list[int], byte_offset: int) -> dict:
    """byte → {line, character}（LSP 标准，character 按 UTF-16 码元计数）。"""
    total = len(text.encode("utf-8"))
    byte_offset = min(byte_offset, total)
    # 二分查找行
    lo, hi = 0, len(line_starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= byte_offset:
            lo = mid + 1
        else:
            hi = mid
    line = max(0, lo - 1)
    line_start = line_starts[line]
    col_byte = byte_offset - line_start
    line_end = line_starts[line + 1] if line + 1 < len(line_starts) else total
    line_bytes = text.encode("utf-8")[line_start:line_end]
    line_text = line_bytes.decode("utf-8", errors="ignore")
    # UTF-16 码元计数
    byte_cursor = 0
    utf16 = 0
    for ch in line_text:
        if byte_cursor >= col_byte:
            break
        byte_cursor += len(ch.encode("utf-8"))
        utf16 += len(ch.encode("utf-16-le")) // 2
    return {"line": line, "character": utf16}


def _position_to_byte(text: str, line_starts: list[int], line: int, character: int) -> int:
    """{line, character} → byte（UTF-16 码元 → 字节）。"""
    total = len(text.encode("utf-8"))
    if line >= len(line_starts):
        return total
    line_start = line_starts[line]
    line_end = line_starts[line + 1] if line + 1 < len(line_starts) else total
    line_bytes = text.encode("utf-8")[line_start:line_end]
    line_text = line_bytes.decode("utf-8", errors="ignore")
    utf16_count = 0
    byte_offset = 0
    for ch in line_text:
        if utf16_count >= character:
            break
        utf16_count += len(ch.encode("utf-16-le")) // 2
        byte_offset += len(ch.encode("utf-8"))
    return line_start + byte_offset


def _tool_lsp_position_convert(arguments: dict) -> list[types.TextContent]:
    """byte ↔ line:col 双向转换（FastLineIndex 移植，LSP 标准 UTF-16 码元）。"""
    text = arguments.get("text", "")
    if len(text) > _SYMBOL_CONTENT_LIMIT:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "text 超过 1MB 上限（防 O(n) 编码 DoS，security sa_20260809_004631）"},
            ensure_ascii=False))]
    direction = arguments.get("direction", "byte_to_position")
    line_starts = _build_line_starts(text)
    total = len(text.encode("utf-8"))
    if direction == "byte_to_position":
        byte_offset = int(arguments.get("byte_offset", 0))
        pos = _byte_to_position(text, line_starts, byte_offset)
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "direction": "byte_to_position", "byte_offset": byte_offset,
             "position": pos, "total_bytes": total}, ensure_ascii=False, indent=2))]
    line = int(arguments.get("line", 0))
    character = int(arguments.get("character", 0))
    byte_offset = _position_to_byte(text, line_starts, line, character)
    return [types.TextContent(type="text", text=json.dumps(
        {"ok": True, "direction": "position_to_byte", "position": {"line": line, "character": character},
         "byte_offset": byte_offset, "total_bytes": total}, ensure_ascii=False, indent=2))]


def _tool_lsp_semantic_tokens_decode(arguments: dict) -> list[types.TextContent]:
    """LSP semantic tokens 解码（SemanticTokensDecoder::decode 移植）。"""
    data = arguments.get("data", [])
    # 兼容字符串数组输入；限长度防 OOM（security 审查修复）
    if isinstance(data, str):
        if len(data) > 1_000_000:
            return [types.TextContent(type="text", text=json.dumps(
                {"ok": False, "error": "data 字符串超过 1MB 上限（防 OOM）"}, ensure_ascii=False))]
        import ast as _ast_mod
        try:
            data = _ast_mod.literal_eval(data)
        except (ValueError, SyntaxError, MemoryError):
            return [types.TextContent(type="text", text=json.dumps(
                {"ok": False, "error": "data 无法解析为字面量"}, ensure_ascii=False))]
    if not isinstance(data, list):
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "data 必须是整数数组"}, ensure_ascii=False))]
    if len(data) > 200_000:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "data 元素超过 200000 上限（防 OOM）"}, ensure_ascii=False))]
    tokens = []
    current_line = 0
    current_char = 0
    for chunk in [data[i:i + 5] for i in range(0, len(data) - 4, 5)]:
        if len(chunk) < 5:
            break
        delta_line, delta_start, length, token_type, token_modifiers = chunk[:5]
        if delta_line > 0:
            current_line += delta_line
            current_char = delta_start
        else:
            current_char += delta_start
        tokens.append({
            "line": current_line, "start_char": current_char,
            "length": length, "token_type": token_type, "token_modifiers": token_modifiers,
        })
    return [types.TextContent(type="text", text=json.dumps(
        {"ok": True, "token_count": len(tokens), "tokens": tokens[:100],
         "note": "每 5 个 uinteger 描述一个 token: [deltaLine, deltaStartChar, length, tokenType, tokenModifiers]"},
        ensure_ascii=False, indent=2))]


def _tool_lsp_edit_merge(arguments: dict) -> list[types.TextContent]:
    """相邻编辑合并（IncrementalChangeCalculator::merge_edits 移植，H-22 修正同款）。

    edits: [{range: {start: {line, character}, end: {line, character}}, text}]
    仅合并真正相邻（next.start == current.end）的编辑，避免丢文本。
    """
    edits = arguments.get("edits", [])
    if len(edits) <= 1:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "merged": edits, "merged_count": len(edits)}, ensure_ascii=False, indent=2))]
    merged = []
    current = dict(edits[0])
    for edit in edits[1:]:
        cur_rng = current.get("range")
        nxt_rng = edit.get("range")
        if cur_rng and nxt_rng:
            cur_end = cur_rng.get("end", {})
            nxt_start = nxt_rng.get("start", {})
            is_adjacent = (nxt_start.get("line") == cur_end.get("line")
                           and nxt_start.get("character") == cur_end.get("character"))
            if is_adjacent:
                current = {
                    "range": {"start": cur_rng.get("start"), "end": nxt_rng.get("end")},
                    "text": current.get("text", "") + edit.get("text", ""),
                }
                continue
        merged.append(current)
        current = dict(edit)
    merged.append(current)
    return [types.TextContent(type="text", text=json.dumps(
        {"ok": True, "merged": merged, "merged_count": len(merged),
         "original_count": len(edits)}, ensure_ascii=False, indent=2))]


# ───────────────────────── 7. Aether provider 探测 + LSP 交互 ─────────────────────────
# aether_model_provider: 移植 aether-ai AiProvider 预设（解决"LLM 客户端冲突"：
# 不做模型调用，只做配置探测，让 RX 可选）
# lsp_query: 通用 LSP 客户端（JSON-RPC over stdio，Content-Length 帧），
# spawn 语言服务器子进程（rust-analyzer/pylsp/clangd/gopls），提供
# completion/hover/definition/references——Aether default_server_config 同款

import subprocess as _subprocess
import threading as _threading
import queue as _queue

# Aether AiProvider 预设（aether-ai/src/lib.rs 移植）
AETHER_PROVIDERS = [
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-pro",
        "preset_models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    },
    {
        "name": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "preset_models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-latest"],
    },
    {
        "name": "custom",
        "base_url": "",
        "default_model": "",
        "preset_models": [],
    },
]

# Aether default_server_config 语言服务器表（client.rs:621 移植）
LSP_SERVER_CONFIG = {
    "rust": ("rust-analyzer", []),
    "python": ("pylsp", []),
    "typescript": ("typescript-language-server", ["--stdio"]),
    "javascript": ("typescript-language-server", ["--stdio"]),
    "c": (r"C:\Program Files\LLVM\bin\clangd.exe", []),
    "cpp": (r"C:\Program Files\LLVM\bin\clangd.exe", []),
}


def _tool_aether_model_provider(arguments: dict) -> list[types.TextContent]:
    """Aether 模型服务商配置探测（aether-ai AiProvider 移植）。

    为什么不做"调用"：aether-ai 本身是 OpenAI 兼容 LLM 客户端，嵌进 MCP 会
    形成"模型调模型"——上下文不共享、费用失控、主循环语义混乱（冲突点）。
    解法：只暴露 provider/base_url/preset_models 配置，RX 自己选模型调用。
    """
    want = (arguments.get("provider") or "").lower()
    providers = [p for p in AETHER_PROVIDERS if p["name"] == want] if want else AETHER_PROVIDERS
    return [types.TextContent(type="text", text=json.dumps(
        {
            "ok": True,
            "providers": providers,
            "note": "冲突解法：不做 LLM 调用（模型调模型），只做配置探测——RX 可直接用这些 base_url/模型名调自身 provider",
        }, ensure_ascii=False, indent=2))]


class _LspClient:
    """极简 LSP 客户端：Content-Length 帧 JSON-RPC over stdio。"""

    def __init__(self, command: str, args: list[str], root: str):
        # root 可能不存在（如 C:\tmp），自动创建或回退到可写目录
        cwd = root or None
        if cwd:
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                cwd = os.getcwd()
        # Windows 下 .CMD/.BAT 需经 cmd /c 包装，否则 Popen 直接失败；
        # 且必须先解析完整路径（cmd 不继承 Python 的 PATH 解析逻辑）
        resolved = command
        if os.name == "nt":
            p = shutil.which(command)
            if p:
                resolved = p
        if os.name == "nt" and (resolved.lower().endswith(".cmd") or resolved.lower().endswith(".bat")):
            launch = ["cmd", "/c", resolved] + args
        else:
            launch = [resolved] + args
        self.proc = _subprocess.Popen(
            launch, stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
            stderr=_subprocess.DEVNULL, cwd=cwd, text=False,
            bufsize=0,
        )
        self._msg_id = 0
        # 单一常驻 reader 线程 + 按 id 分发：响应/通知都进 _inbox，
        # 请求等待自己 id 的响应；超时不泄漏线程、帧不会被并发读分割。
        self._inbox: _queue.Queue = _queue.Queue()
        self._closed = False
        self._reader = _threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def _reader_loop(self) -> None:
        """常驻帧读取线程：解析 Content-Length 帧 → 投递到 _inbox。"""
        try:
            while not self._closed:
                headers = b""
                while b"\r\n\r\n" not in headers:
                    b = self.proc.stdout.read(1)
                    if not b:
                        self._inbox.put(None)
                        return
                    headers += b
                    if len(headers) > 65536:
                        self._inbox.put(None)
                        return
                m = re.search(rb"Content-Length: (\d+)", headers, re.I)
                if not m:
                    self._inbox.put(None)
                    return
                length = int(m.group(1))
                if length > 16 * 1024 * 1024:
                    self._inbox.put(None)
                    return
                body = self.proc.stdout.read(length)
                if not body:
                    self._inbox.put(None)
                    return
                try:
                    self._inbox.put(json.loads(body.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._inbox.put(None)
        except Exception:
            self._inbox.put(None)

    def _send(self, obj: dict) -> bool:
        """发送帧；失败（进程已退出/管道关闭）返回 False，不抛异常。"""
        try:
            body = json.dumps(obj).encode("utf-8")
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self.proc.stdin.write(header + body)
            self.proc.stdin.flush()
            return True
        except (OSError, BrokenPipeError, ValueError):
            return False

    def request(self, method: str, params: dict, timeout: float = 25.0) -> dict:
        """请求级绝对截止时间：持续推送通知不重置超时（review H 修复）。"""
        import time as _time
        self._msg_id += 1
        my_id = self._msg_id
        if not self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params}):
            return {"error": "语言服务器已退出"}
        deadline = _time.monotonic() + timeout
        while True:
            remain = deadline - _time.monotonic()
            if remain <= 0:
                return {"error": f"语言服务器响应超时（>{timeout}s）"}
            try:
                frame = self._inbox.get(timeout=remain)
            except _queue.Empty:
                return {"error": f"语言服务器响应超时（>{timeout}s）"}
            if frame is None:
                return {"error": "语言服务器已退出"}
            if isinstance(frame, dict) and frame.get("id") == my_id:
                return frame
            # notification / 其他 id 的响应：丢弃，继续等自己的

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, root: str, language_id: str) -> dict:
        resp = self.request("initialize", {
            "processId": None,
            "rootUri": _path_to_uri(root) if root else None,
            "capabilities": {},
            "workspaceFolders": [{"uri": _path_to_uri(root), "name": "root"}] if root else None,
        })
        self.notify("initialized", {})
        return resp

    def close(self) -> None:
        self._closed = True
        try:
            if os.name == "nt":
                # Windows 下用 taskkill /T 清理整个进程树（node 子进程）
                _subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                capture_output=True, timeout=5)
            else:
                self.proc.terminate()
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _path_to_uri(p: str) -> str:
    # 相对路径 → 绝对路径（LSP URI 必须是 file:/// 绝对形式，否则 rust-analyzer 报 "url is not a file"）
    if p and not os.path.isabs(p):
        p = os.path.abspath(p)
    # 转义 URI 保留字符（# % 等，review 建议），用 urllib.parse.quote
    from urllib.parse import quote
    return "file:///" + quote(p.replace("\\", "/"), safe="/:")


def _tool_lsp_query(arguments: dict) -> list[types.TextContent]:
    """LSP 交互查询：spawn 语言服务器 → completion/hover/definition/references。

    language_id 支持 rust/python/typescript/javascript/c/cpp（Aether 配置表）。
    仅当对应语言服务器已安装才可用（rust-analyzer 已装；pylsp/clangd 需安装）。
    文档文本与请求在单次调用内完成，服务器进程用完即关（无状态）。
    """
    language_id = arguments.get("language_id", "").lower()
    request_type = arguments.get("request", "hover")
    path = arguments.get("path", "")
    line = int(arguments.get("line", 0))
    character = int(arguments.get("character", 0))
    text = arguments.get("text", "")
    if len(text) > _SYMBOL_CONTENT_LIMIT:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "text 超过 1MB 上限（security sa_20260809_004631）"}, ensure_ascii=False))]
    root = arguments.get("root", "")

    if language_id not in LSP_SERVER_CONFIG:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"不支持的语言: {language_id}",
             "supported": list(LSP_SERVER_CONFIG.keys())}, ensure_ascii=False))]
    cmd, args = LSP_SERVER_CONFIG[language_id]
    # 检查命令可用
    if not _command_available(cmd):
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"语言服务器未安装: {cmd}",
             "hint": f"安装后可用: pip install python-lsp-server (pylsp) / 下载 {cmd}"},
            ensure_ascii=False))]

    client = None
    try:
        client = _LspClient(cmd, args, root)
        init = client.initialize(root, language_id)
        if init.get("error"):
            return [types.TextContent(type="text", text=json.dumps(
                {"ok": False, "error": f"initialize 失败: {init['error']}"}, ensure_ascii=False))]
        uri = _path_to_uri(path)
        client.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id, "version": 1, "text": text},
        })
        pos = {"line": line, "character": character}
        if request_type == "completion":
            resp = client.request("textDocument/completion", {
                "textDocument": {"uri": uri}, "position": pos, "context": {"triggerKind": 1},
            })
        elif request_type == "definition":
            resp = client.request("textDocument/definition", {
                "textDocument": {"uri": uri}, "position": pos,
            })
        elif request_type == "references":
            resp = client.request("textDocument/references", {
                "textDocument": {"uri": uri}, "position": pos, "context": {"includeDeclaration": True},
            })
        else:  # hover
            resp = client.request("textDocument/hover", {
                "textDocument": {"uri": uri}, "position": pos,
            })
        # rust-analyzer 首次启动需 1~3s 做项目索引（cargo metadata），期间请求返回
        # -32801 content modified（LSP 规范要求客户端重发请求）。
        # 只对 -32801 重试（result:null 是合法空结果，不重试——review H 修复）。
        if language_id == "rust":
            import time as _time
            _retries = 3
            while _retries > 0:
                _retryable = _is_retryable_error(resp)
                if not _retryable:
                    break
                _time.sleep(1.5)
                resp = client.request("textDocument/" + request_type, {
                    "textDocument": {"uri": uri}, "position": pos,
                })
                _retries -= 1
        # review 复核修复：非 initialize 请求的错误也要检查，
        # 否则超时/服务器错误会被伪装成"无结果"（ok:true, result:null）
        resp_err = resp.get("error")
        if resp_err:
            msg = resp_err.get("message", "") if isinstance(resp_err, dict) else str(resp_err)
            return [types.TextContent(type="text", text=json.dumps(
                {"ok": False, "request": request_type, "language": language_id,
                 "error": f"语言服务器错误: {msg}"}, ensure_ascii=False, indent=2))]
        result = resp.get("result")
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "request": request_type, "language": language_id,
             "position": pos, "result": result}, ensure_ascii=False, indent=2))]
    except FileNotFoundError:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"无法启动语言服务器: {cmd}"}, ensure_ascii=False))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))]
    finally:
        if client:
            client.close()


def _command_available(cmd: str) -> bool:
    # 完整路径（含空格）直接文件存在性判断；否则 where/which
    if os.path.isabs(cmd):
        return os.path.isfile(cmd)
    try:
        if os.name == "nt":
            r = _subprocess.run(["where", cmd], capture_output=True, timeout=5)
        else:
            r = _subprocess.run(["which", cmd], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


async def run():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="code-analysis-enhance",
                server_version="0.1.0",
                capabilities=app.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
            ),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="运行内置自检后退出")
    args, _ = parser.parse_known_args()
    if args.selftest:
        # 自检：三个核心工具的基本路径
        r1 = _tool_file_dedup({"path": os.path.abspath(__file__)})
        r2 = _tool_file_dedup({"path": os.path.abspath(__file__)})
        d1 = json.loads(r1[0].text)
        d2 = json.loads(r2[0].text)
        print("file_dedup: first=", d1["unchanged"], "second=", d2["unchanged"], "(期望 False/True)")
        assert d1["unchanged"] is False and d2["unchanged"] is True, "去重状态机错误"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r3 = _tool_change_impact({"repo_path": repo, "changed_files": ["code-analysis-enhance/server.py"]})
        d3 = json.loads(r3[0].text)
        print("change_impact: ok=", d3["ok"], "symbols=", d3["results"][0]["symbols"][:5])
        assert d3["ok"] and d3["results"][0]["symbols"], "符号提取失败"
        r4 = _tool_lesson_recall({"task_description": "修复 Python 依赖缺失问题"})
        d4 = json.loads(r4[0].text)
        print("lesson_recall: ok=", d4["ok"], "lessons=", len(d4["lessons"]), "antipatterns=", len(d4["antipatterns"]))
        r5 = _tool_aether_probe({})
        d5 = json.loads(r5[0].text)
        print("aether_probe: ok=", d5["ok"], "kind=", d5["kind"])
        print("自检全部通过")
    else:
        import asyncio
        asyncio.run(run())
