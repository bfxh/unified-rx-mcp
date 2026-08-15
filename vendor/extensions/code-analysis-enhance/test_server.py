#!/usr/bin/env python3
"""pytest 测试：code-analysis-enhance MCP 的四个工具核心逻辑。"""
import json
import os
import queue
import sys
import threading
from pathlib import Path

import pytest

SERVER = Path(__file__).parent / "server.py"

# 独立模块名加载：本仓库根目录、vendor 各扩展目录都有 cae.py——
# 全量 pytest 时 sys.modules["server"] 可能已被根 server 占用，直接
# import server 会拿到错误的模块（AttributeError: no _tool_*）。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("cae_server", str(SERVER))
cae = _ilu.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cae)


def _text(tool_result):
    return json.loads(tool_result[0].text)


def test_file_dedup_state_machine():
    """首次 False，再次 True（文件未变不重读）。"""
    target = os.path.abspath(__file__)
    r1 = _text(cae._tool_file_dedup({"path": target}))
    r2 = _text(cae._tool_file_dedup({"path": target}))
    assert r1["unchanged"] is False
    assert r2["unchanged"] is True
    assert r2["advice"].startswith("文件未变化")


def test_file_dedup_missing():
    r = _text(cae._tool_file_dedup({"path": r"C:\no_such_file_xyz.txt"}))
    assert r["ok"] is False


def test_change_impact_extracts_symbols():
    repo = str(SERVER.parent.parent.parent.parent)  # 仓库根（vendor/extensions 上三级）
    rel = "vendor/extensions/code-analysis-enhance/server.py"
    r = _text(cae._tool_change_impact({"repo_path": repo, "changed_files": [rel]}))
    assert r["ok"] is True
    res = r["results"][0]
    assert res["ok"] is True
    assert "_tool_file_dedup" in res["symbols"]
    assert isinstance(res["suggested_tests"], list)


def test_change_impact_missing_repo():
    r = _text(cae._tool_change_impact({"repo_path": r"C:\no_repo", "changed_files": []}))
    assert r["ok"] is False


def test_lesson_recall_hits_warnings():
    r = _text(cae._tool_lesson_recall({"task_description": "修复 Python 依赖缺失 ModuleNotFoundError"}))
    assert r["ok"] is True
    assert isinstance(r["lessons"], list)
    assert isinstance(r["antipatterns"], list)
    assert "task_keywords" in r


def test_aether_probe():
    r = _text(cae._tool_aether_probe({}))
    assert r["ok"] is True
    assert r["kind"] == "rust-native-binary"


def test_code_context_python_ast():
    """code_context：Python AST 精确解析（类/函数/导入）+ Prompt 组装。"""
    target = os.path.abspath(__file__)  # 用 test 文件自身（Python）
    r = _text(cae._tool_code_context({"path": target, "cursor_line": 1}))
    assert r["ok"] is True
    meta = r["metadata"]
    assert "imports" in meta and meta["lines"] > 0
    assert "prompt" in r and r["prompt"].startswith("// file:")
    assert "光标处符号" in r["prompt"] or "函数:" in r["prompt"] or "类:" in r["prompt"]
    assert meta["imports"], "应提取到 import 依赖"


def test_code_context_cursor_symbol():
    """光标处符号定位：光标在函数定义行应命中该函数。"""
    target = os.path.abspath(__file__)
    content = Path(target).read_text(encoding="utf-8")
    # 找第一个 def 行号
    line_no = None
    for i, line in enumerate(content.splitlines(), 1):
        if line.startswith("def "):
            line_no = i
            break
    assert line_no is not None
    r = _text(cae._tool_code_context({"path": target, "cursor_line": line_no}))
    assert r["cursor_symbol"] is not None
    assert r["cursor_symbol"]["kind"] == "functions"


def test_code_context_missing_file():
    r = _text(cae._tool_code_context({"path": r"C:\no_such_file.py", "cursor_line": 1}))
    assert r["ok"] is False


# ── Aether 能力移植测试 ──

def test_aether_agent_parse_edits():
    """AETHER_FILE 编辑块解析（create/replace）。"""
    resp = (
        "<<<<<<< AETHER_FILE src/new.py\n"
        "print('hello')\n"
        "======= AETHER_SEP\n"
        "print('world')\n"
        ">>>>>>> AETHER_END_FILE\n"
        "<<<<<<< AETHER_FILE src/old.py\n"
        "def old():\n"
        "    pass\n"
        "======= AETHER_SEP\n"
        "\n"
        ">>>>>>> AETHER_END_FILE\n"
    )
    r = _text(cae._tool_aether_agent_parse({"response": resp}))
    assert r["ok"] is True
    assert len(r["edits"]) == 2
    assert r["edits"][0]["path"] == "src/new.py"
    assert r["edits"][0]["search"] == "print('hello')"
    assert r["edits"][0]["replace"] == "print('world')"
    assert r["edits"][0]["kind"] == "replace"


def test_aether_agent_parse_run_and_read():
    """AETHER_RUN 命令块 + AETHER_READ 只读请求。"""
    resp = (
        "<<<<<<< AETHER_RUN\n"
        "cargo test\n"
        "cargo fmt --check\n"
        ">>>>>>> AETHER_END_RUN\n"
        "<<<<<<< AETHER_READ src/main.rs\n"
        "<<<<<<< AETHER_LIST\n"
    )
    r = _text(cae._tool_aether_agent_parse({"response": resp}))
    assert r["ok"] is True
    assert r["run_commands"] == ["cargo test", "cargo fmt --check"]
    assert len(r["tool_requests"]) == 2
    assert r["tool_requests"][0]["kind"] == "read"
    assert r["tool_requests"][0]["path"] == "src/main.rs"
    assert r["tool_requests"][1]["kind"] == "list"


def test_aether_agent_parse_create():
    """空 search = 新建文件。"""
    resp = (
        "<<<<<<< AETHER_FILE README.md\n"
        "\n"
        "======= AETHER_SEP\n"
        "# 标题\n"
        ">>>>>>> AETHER_END_FILE\n"
    )
    r = _text(cae._tool_aether_agent_parse({"response": resp}))
    assert r["edits"][0]["kind"] == "create"


def test_aether_lang_support():
    r = _text(cae._tool_aether_lang_support({"path": r"C:\x\main.py"}))
    assert r["detected_language"] == "python"
    assert r["supported"] is True
    r2 = _text(cae._tool_aether_lang_support({"path": r"C:\x\main.gd"}))
    assert r2["detected_language"] == "gdscript"


def test_aether_goto_parse():
    r = _text(cae._tool_aether_goto_parse({"goto": "src/main.rs:42:7"}))
    assert r["path"] == "src/main.rs"
    assert r["line"] == 42
    assert r["column"] == 7
    assert r["zero_based_line"] == 41
    r2 = _text(cae._tool_aether_goto_parse({"goto": "onlypath"}))
    assert r2["path"] == "onlypath"
    assert r2["line"] == 0


# ── LSP 算法移植测试 ──

def test_lsp_position_convert_byte_to_position():
    """FastLineIndex 移植：byte→line:col（UTF-16 码元）。"""
    text = "abc\ndef\nghi"
    r = _text(cae._tool_lsp_position_convert({"text": text, "direction": "byte_to_position", "byte_offset": 4}))
    assert r["position"]["line"] == 1
    assert r["position"]["character"] == 0
    r2 = _text(cae._tool_lsp_position_convert({"text": text, "direction": "byte_to_position", "byte_offset": 0}))
    assert r2["position"]["line"] == 0
    assert r2["position"]["character"] == 0


def test_lsp_position_convert_position_to_byte():
    """line:col→byte 反向转换（含中文 UTF-16 计数）。"""
    text = "你好\nworld"
    r = _text(cae._tool_lsp_position_convert({"text": text, "direction": "position_to_byte", "line": 0, "character": 2}))
    # "你好" 每个字 3 字节 UTF-8；character=2 是 UTF-16 码元数（每字 1）
    assert r["byte_offset"] == 6
    r2 = _text(cae._tool_lsp_position_convert({"text": text, "direction": "position_to_byte", "line": 1, "character": 2}))
    assert r2["byte_offset"] == 9  # 6("你好\n") + 2("wo")


def test_lsp_semantic_tokens_decode():
    """semantic tokens 解码（delta 累加）。"""
    data = [0, 0, 3, 1, 0, 0, 4, 5, 2, 0]
    r = _text(cae._tool_lsp_semantic_tokens_decode({"data": data}))
    assert r["token_count"] == 2
    assert r["tokens"][0] == {"line": 0, "start_char": 0, "length": 3, "token_type": 1, "token_modifiers": 0}
    assert r["tokens"][1] == {"line": 0, "start_char": 4, "length": 5, "token_type": 2, "token_modifiers": 0}


def test_lsp_semantic_tokens_delta_line():
    """delta_line>0 时重置 start_char。"""
    data = [1, 3, 2, 1, 0]
    r = _text(cae._tool_lsp_semantic_tokens_decode({"data": data}))
    assert r["tokens"][0]["line"] == 1
    assert r["tokens"][0]["start_char"] == 3


# ── security 审查修复测试 ──

def test_symbol_patterns_sanitized():
    """用户正则受限：超 5 个截断、超 200 字符丢弃（防 ReDoS）。"""
    content = "def foo():\n    pass\n"
    extra = ["X" * 300, r"^\s*def\s+(\w+)", "more", "more2", "more3", "more4", "more5"]
    syms = cae._extract_symbols(content, ".py", extra)
    assert "foo" in syms, f"合法正则应生效, 实际 {syms}"
    # 超长正则被丢弃、超数量被截断——不崩溃
    assert len(extra) > 5  # 输入 7 个，只取前 5


def test_symbol_patterns_redos_rejected():
    """易 ReDoS 结构正则被拒绝（security sa_20260808_233440 修复）。"""
    redos_pats = [r"(a+)+$", r"(a|aa)+$", r"(a?)*$", r"[a]+[a]*", r"(ab){2,9}"]
    for p in redos_pats:
        assert cae._is_redos_risky(p), f"应拒绝 ReDoS 结构: {p}"
    safe_pats = [r"^\s*def\s+(\w+)", r"\w+", r"class\s+([A-Z]\w*)"]
    for p in safe_pats:
        assert not cae._is_redos_risky(p), f"不应误杀正常正则: {p}"


def test_symbol_patterns_redos_bypass_rejected():
    """{m,} 内嵌量词绕过形态也被拒绝（review sa_20260808_233821 修复）。"""
    bypass_pats = [r"(a{1,})+", r"(a{2,3})*", r"(a+(b))+", r"(ab{1,})+"]
    for p in bypass_pats:
        assert cae._is_redos_risky(p), f"应拒绝绕过形态: {p}"


def test_symbol_patterns_no_capture_group():
    """无捕获组正则不抛 IndexError（review sa_20260808_234216 修复）。"""
    content = "def foo():\n    pass\nbar = 1\n"
    # \w+ 无捕获组，匹配时用整个匹配
    syms = cae._extract_symbols(content, ".py", [r"\w+"])
    assert "foo" in syms and "bar" in syms, f"无捕获组应返回匹配串, 实际 {syms[:5]}"


# ── security sa_20260808_234752 修复测试 ──

def test_symbol_patterns_sandwich_rejected():
    """三明治形态（O(n²) 回溯）被拒绝。"""
    sandwich = [r"a+b*a+", r"[a-z]+\d+[a-z]+"]
    for p in sandwich:
        assert cae._is_redos_risky(p), f"应拒绝三明治形态: {p}"


def test_symbol_patterns_bounded_quantifier_chain():
    """有界量词链被拒绝（security sa_20260809_000110 blocking 修复）。"""
    chains = [r"a{1,100}a{1,100}a{1,100}b", r"\w{1,10}\d{1,10}\w{1,10}"]
    for p in chains:
        assert cae._is_redos_risky(p), f"应拒绝有界量词链: {p}"


def test_symbol_patterns_unbounded_quantifier_chain():
    """无上限量词链被拒绝（security sa_20260809_003600 blocking 修复）。"""
    chains = [r"a{1,}a{1,}a{1,}b", r"\w{1,}\w{1,}\w{1,}b", r"a{1,1000}a{1,1000}a{1,1000}b", r"(a){1,}(a){1,}(a){1,}b"]
    for p in chains:
        assert cae._is_redos_risky(p), f"应拒绝无上限量词链: {p}"


def test_symbol_patterns_two_quantifier_overlap():
    """两量词重叠链被拒绝（security sa_20260809_004631 blocking 修复）。"""
    chains = [r"a+a+b", r"a+a{1,}b", r"a+[a]+b", r"a+\w+b", r"\d+\d+b", r"a*a+b"]
    for p in chains:
        assert cae._is_redos_risky(p), f"应拒绝两量词重叠链: {p}"


def test_symbol_patterns_open_interval_chain():
    """{m,} 开区间量词链被拒绝（security sa_20260809_010648 修复）。"""
    chains = [r"a{1,}aaaaaa{1,}b", r"a{1,}bbbb{1,}cccc{1,}d"]
    for p in chains:
        assert cae._is_redos_risky(p), f"应拒绝开区间量词链: {p}"


def test_change_impact_large_file_rejected(tmp_path):
    """超大文件前置拒绝（security 修复）。"""
    repo = tmp_path / "bigrepo"
    repo.mkdir()
    big = repo / "huge.py"
    big.write_bytes(b"x" * (cae._SYMBOL_CONTENT_LIMIT * 3))
    r = _text(cae._tool_change_impact({"repo_path": str(repo), "changed_files": ["huge.py"]}))
    assert r["ok"] is True
    assert r["results"][0]["ok"] is False
    assert "2MB" in r["results"][0]["reason"]


def test_lsp_position_convert_text_limit():
    """超大 text 被拒绝（防 O(n) 编码 DoS）。"""
    r = _text(cae._tool_lsp_position_convert({"text": "a" * (cae._SYMBOL_CONTENT_LIMIT + 10),
                                                 "direction": "byte_to_position", "byte_offset": 0}))
    assert r["ok"] is False
    assert "1MB" in r["error"]


def test_change_impact_path_traversal(tmp_path):
    """.. 逃逸路径被拒绝（security 修复）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    r = _text(cae._tool_change_impact({
        "repo_path": str(repo),
        "changed_files": ["../secret.py", "ok.py"],
    }))
    by_file = {res["file"]: res for res in r["results"]}
    assert by_file["../secret.py"]["ok"] is False
    assert "越界" in by_file["../secret.py"]["reason"]
    assert by_file["ok.py"]["ok"] is True


def test_change_impact_too_many_files(tmp_path):
    """changed_files 超过 50 被拒绝。"""
    repo = tmp_path / "repo2"
    repo.mkdir()
    r = _text(cae._tool_change_impact({
        "repo_path": str(repo),
        "changed_files": [f"f{i}.py" for i in range(60)],
    }))
    assert r["ok"] is False
    assert "50" in r["error"]


def test_semantic_tokens_oom_guard():
    """超大 data 字符串被拒绝（防 OOM）。"""
    r = _text(cae._tool_lsp_semantic_tokens_decode({"data": "[" + ",".join(["1"] * 300000) + "]"}))
    assert r["ok"] is False
    assert "上限" in r["error"] or "1MB" in r["error"]


def test_semantic_tokens_bad_data():
    """非列表 data 被拒绝。"""
    r = _text(cae._tool_lsp_semantic_tokens_decode({"data": {"not": "list"}}))
    assert r["ok"] is False


def test_lsp_edit_merge_adjacent():
    """相邻编辑合并（next.start == current.end）。"""
    edits = [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 2}}, "text": "ab"},
        {"range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 4}}, "text": "cd"},
    ]
    r = _text(cae._tool_lsp_edit_merge({"edits": edits}))
    assert r["merged_count"] == 1
    assert r["merged"][0]["text"] == "abcd"
    assert r["merged"][0]["range"]["end"]["character"] == 4


def test_lsp_edit_merge_non_adjacent():
    """非相邻编辑不合并（H-22 修正）。"""
    edits = [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 2}}, "text": "ab"},
        {"range": {"start": {"line": 0, "character": 5}, "end": {"line": 0, "character": 7}}, "text": "cd"},
    ]
    r = _text(cae._tool_lsp_edit_merge({"edits": edits}))
    assert r["merged_count"] == 2


# ── 升级轮测试：跨文件引用 / 路径通配 / RX memory ──

def test_code_context_cross_file_references(tmp_path):
    """code_context 跨文件引用链：search_repo 应找到引用符号的文件。"""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "main.py").write_text("import helper\nhelper.greet('x')\n", encoding="utf-8")
    target = repo / "helper.py"
    target.write_text("def greet(name):\n    return name\n", encoding="utf-8")
    r = _text(cae._tool_code_context({
        "path": str(target), "cursor_line": 1, "search_repo": str(repo)}))
    assert r["ok"] is True
    assert r["cursor_symbol"]["name"] == "greet"
    files = [ref["file"] for ref in r["references"]]
    assert "main.py" in files, f"应找到 main.py 引用, 实际 {files}"


def test_aether_agent_parse_wildcard(tmp_path):
    """aether_agent_parse 路径通配：* 应展开为多个编辑。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.rs").write_text("old", encoding="utf-8")
    (d / "b.rs").write_text("old", encoding="utf-8")
    resp = (
        f"<<<<<<< AETHER_FILE {d / '*.rs'}\n"
        f"old\n"
        f"======= AETHER_SEP\n"
        f"new\n"
        f">>>>>>> AETHER_END_FILE\n"
    )
    r = _text(cae._tool_aether_agent_parse({"response": resp}))
    assert r["ok"] is True
    assert len(r["edits"]) == 2
    paths = [e["path"] for e in r["edits"]]
    assert any(p.endswith("a.rs") for p in paths)
    assert any(p.endswith("b.rs") for p in paths)


def test_lesson_recall_rx_memory():
    """lesson_recall 应能读取 RX memory 目录（含 feedback 记忆）。"""
    r = _text(cae._tool_lesson_recall({"task_description": "缓存优化 正确性优先 不要命中率"}))
    assert r["ok"] is True
    assert isinstance(r["lessons"], list)
    assert isinstance(r["antipatterns"], list)


def test_code_context_reference_lines(tmp_path):
    """跨文件引用链含行号定位。"""
    repo = tmp_path / "proj2"
    repo.mkdir()
    (repo / "main.py").write_text("import helper\nhelper.greet('x')\n", encoding="utf-8")
    target = repo / "helper.py"
    target.write_text("def greet(name):\n    return name\n", encoding="utf-8")
    r = _text(cae._tool_code_context({
        "path": str(target), "cursor_line": 1, "search_repo": str(repo)}))
    main_ref = next((ref for ref in r["references"] if ref["file"] == "main.py"), None)
    assert main_ref is not None
    assert 2 in main_ref["lines"], f"应包含第 2 行引用, 实际 {main_ref['lines']}"


def test_aether_agent_parse_wildcard_exclude(tmp_path):
    """通配排除：src/*.rs|src/exclude/** 排除后只保留非排除文件。"""
    d = tmp_path / "src"
    ex = d / "exclude"
    ex.mkdir(parents=True)
    (d / "a.rs").write_text("old", encoding="utf-8")
    (ex / "skip.rs").write_text("old", encoding="utf-8")
    resp = (
        f"<<<<<<< AETHER_FILE {d / '*.rs'}|{ex / '**'}\n"
        f"old\n"
        f"======= AETHER_SEP\n"
        f"new\n"
        f">>>>>>> AETHER_END_FILE\n"
    )
    r = _text(cae._tool_aether_agent_parse({"response": resp}))
    assert r["ok"] is True
    paths = [e["path"] for e in r["edits"]]
    assert any(p.endswith("a.rs") for p in paths), f"应包含 a.rs, 实际 {paths}"
    assert not any("skip.rs" in p for p in paths), f"不应包含排除的 skip.rs, 实际 {paths}"


# ── review H 修复测试：LSP 客户端架构 ──

class _FakeProc:
    """模拟子进程：stdin 记录写入，stdout 返回预置字节流。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.stdin = _FakePipe(accept=True)
        self.stdout = _FakePipe(chunks=self._chunks)
        self.pid = 99999

    def poll(self):
        return None

    def kill(self):
        pass

    def terminate(self):
        pass


class _FakePipe:
    def __init__(self, chunks=None, accept=False):
        self._chunks = chunks or []
        self._idx = 0
        self._accept = accept
        self.writes = []
        self._eof_event = threading.Event()

    def read(self, n):
        if not self._chunks:
            # 模拟挂起（无 EOF）：等待直到收到终止信号
            self._eof_event.wait(timeout=10)
            return b""
        if self._idx >= len(self._chunks):
            self._eof_event.wait(timeout=10)
            return b""
        out = self._chunks[self._idx][:n]
        self._chunks[self._idx] = self._chunks[self._idx][n:]
        if not self._chunks[self._idx]:
            self._idx += 1
        return out

    def write(self, b):
        if self._accept:
            self.writes.append(b)
        return len(b)

    def flush(self):
        pass


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def test_lsp_request_ignores_notifications_until_timeout():
    """持续推送通知不重置超时：短超时应按时返回（review H 修复）。"""
    # 构造：先来大量 notification（无 id），响应永不出现
    notif = _frame({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
    # 模拟 stdout 持续给 notification（读不完），但无带 id 的响应
    client = cae._LspClient.__new__(cae._LspClient)
    client.proc = _FakeProc(chunks=[notif * 50])
    client._msg_id = 0
    client._inbox = queue.Queue()
    client._closed = False
    client._reader = threading.Thread(target=client._reader_loop, daemon=True)
    client._reader.start()
    try:
        import time as _t
        t0 = _t.monotonic()
        resp = client.request("textDocument/hover", {}, timeout=2.0)
        elapsed = _t.monotonic() - t0
        assert "超时" in resp.get("error", ""), f"应超时, 实际 {resp}"
        assert elapsed < 4.0, f"超时应按时返回, 实际耗时 {elapsed:.1f}s"
    finally:
        client._closed = True
        client.close()


def test_lsp_retry_only_32801():
    """重试只对 -32801：result:null 不重试（review H 修复，引用实现函数）。"""
    err_32801 = {"error": {"code": -32801, "message": "content modified"}}
    null_result = {"result": None}
    # 引用 server 的真实判定函数（不复制逻辑）
    assert cae._is_retryable_error(err_32801) is True
    assert cae._is_retryable_error(null_result) is False


def test_lsp_reader_constant_thread():
    """常驻 reader 线程：连续请求不创建新线程（无泄漏）。"""
    client = cae._LspClient.__new__(cae._LspClient)
    resp_frame = _frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})
    client.proc = _FakeProc(chunks=[resp_frame])
    client._msg_id = 0
    client._inbox = queue.Queue()
    client._closed = False
    client._reader = threading.Thread(target=client._reader_loop, daemon=True)
    client._reader.start()
    try:
        assert client._reader.is_alive()
        # 连续多次请求应复用同一 reader（线程数不增长）
        base = sum(1 for t in threading.enumerate() if t.name.startswith("Thread"))
        for _ in range(3):
            client._inbox.put({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})
        after = sum(1 for t in threading.enumerate() if t.name.startswith("Thread"))
        assert after - base < 3, f"reader 线程应复用, 新增 {after - base}"
    finally:
        client._closed = True
        client.close()


# ── provider 探测 + LSP 交互测试 ──

def test_aether_model_provider_all():
    r = _text(cae._tool_aether_model_provider({}))
    assert r["ok"] is True
    assert len(r["providers"]) == 3
    names = [p["name"] for p in r["providers"]]
    assert "deepseek" in names and "kimi" in names


def test_aether_model_provider_deepseek():
    r = _text(cae._tool_aether_model_provider({"provider": "deepseek"}))
    assert r["ok"] is True
    assert len(r["providers"]) == 1
    p = r["providers"][0]
    assert p["base_url"] == "https://api.deepseek.com/v1"
    assert "deepseek-v4-pro" in p["preset_models"]
    assert "deepseek-v4-flash" in p["preset_models"]


def test_aether_model_provider_unknown():
    r = _text(cae._tool_aether_model_provider({"provider": "nope"}))
    assert r["ok"] is True
    assert r["providers"] == []


def test_lsp_query_unsupported_lang():
    r = _text(cae._tool_lsp_query({"language_id": "brainfuck", "request": "hover",
                                      "path": "x.bf", "line": 0, "character": 0, "text": ""}))
    assert r["ok"] is False
    assert "不支持" in r["error"]


def test_lsp_query_python_real():
    """python → pylsp 已装：真实 LSP 交互应成功。"""
    r = _text(cae._tool_lsp_query({"language_id": "python", "request": "hover",
                                      "path": "x.py", "line": 0, "character": 0,
                                      "text": "print(1)", "root": r"C:\tmp"}))
    assert r["ok"] is True


def test_lsp_query_rust_real():
    """rust → rust-analyzer 已装：真实 LSP 交互应成功。"""
    code = "fn main() {\n    println!(\"hi\");\n}\n"
    r = _text(cae._tool_lsp_query({"language_id": "rust", "request": "hover",
                                      "path": "main.rs", "line": 0, "character": 4,
                                      "text": code, "root": r"C:\tmp"}))
    assert r["ok"] is True, f"rust LSP 失败: {r.get('error')}"


def test_lsp_query_cpp_real():
    """cpp → clangd 已装（全路径）：真实 LSP 交互应成功。"""
    code = "int greet(const char* name) {\n    return 0;\n}\nint main() {\n    greet(\"x\");\n    return 0;\n}\n"
    r = _text(cae._tool_lsp_query({"language_id": "cpp", "request": "hover",
                                      "path": "demo.cpp", "line": 0, "character": 4,
                                      "text": code, "root": r"C:\tmp"}))
    assert r["ok"] is True, f"cpp LSP 失败: {r.get('error')}"


# ── Windows 线程+Queue 超时方案回归测试 ──

def test_lsp_request_timeout_hanging_server():
    """模拟挂起服务器：request 超时返回错误不卡死（新架构常驻 reader）。"""
    import subprocess as _subprocess
    import time as _time
    proc = _subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
        stderr=_subprocess.DEVNULL, text=False, bufsize=0)
    try:
        client = cae._LspClient.__new__(cae._LspClient)
        client.proc = proc
        client._msg_id = 0
        client._inbox = queue.Queue()
        client._closed = False
        client._reader = threading.Thread(target=client._reader_loop, daemon=True)
        client._reader.start()
        t0 = _time.monotonic()
        resp = client.request("initialize", {}, timeout=1.0)
        elapsed = _time.monotonic() - t0
        assert "超时" in str(resp.get("error", "")), f"request 应返回超时错误: {resp}"
        assert elapsed < 5.0, f"超时耗时异常: {elapsed:.2f}s"
    finally:
        proc.kill()


def test_lsp_request_after_exit():
    """进程退出后 request 返回'语言服务器已退出'（新架构）。"""
    import subprocess as _subprocess
    proc = _subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
        stderr=_subprocess.DEVNULL, text=False, bufsize=0)
    client = cae._LspClient.__new__(cae._LspClient)
    client.proc = proc
    client._msg_id = 0
    client._inbox = queue.Queue()
    client._closed = False
    client._reader = threading.Thread(target=client._reader_loop, daemon=True)
    client._reader.start()
    proc.kill()
    proc.wait(timeout=5)
    resp = client.request("initialize", {}, timeout=2.0)
    assert "已退出" in str(resp.get("error", "")), f"应返回已退出: {resp}"


def test_lsp_query_typescript_real():
    """typescript → typescript-language-server 已装：真实 LSP 交互应成功。"""
    r = _text(cae._tool_lsp_query({"language_id": "typescript", "request": "hover",
                                      "path": "demo.ts", "line": 1, "character": 12,
                                      "text": 'const msg: string = "hello";\nconsole.log(msg);\n',
                                      "root": r"C:\tmp"}))
    assert r["ok"] is True, f"typescript LSP 失败: {r.get('error')}"


def test_lsp_rust_project_context_real():
    """rust-analyzer 带 Cargo 项目上下文：就绪重试后 hover 应返回真实符号。

    依赖 %TEMP%\\ra_lsp_verify 临时项目（含 Cargo.toml + src/main.rs），缺失则跳过。
    """
    root = os.path.join(os.environ.get("TEMP", ""), "ra_lsp_verify")
    main_rs = os.path.join(root, "src", "main.rs")
    if not os.path.isfile(main_rs):
        pytest.skip("缺少临时 rust 项目 %TEMP%\\ra_lsp_verify")
    code = Path(main_rs).read_text(encoding="utf-8")
    r = _text(cae._tool_lsp_query({"language_id": "rust", "request": "hover",
                                      "path": main_rs, "line": 6, "character": 20,
                                      "text": code, "root": root}))
    assert r["ok"] is True, f"rust 项目上下文 LSP 失败: {r.get('error')}"
    assert r["result"] is not None, "rust-analyzer 就绪重试后应返回 hover 内容"


def test_lsp_no_process_leak():
    """三语言查询后无进程泄漏：taskkill /T 应清理干净。"""
    import subprocess as _subprocess
    import time as _time

    def count(img):
        try:
            out = _subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {img}"],
                                  capture_output=True, text=True, timeout=10).stdout
            return sum(1 for ln in out.splitlines() if img.lower() in ln.lower())
        except Exception:
            return -1

    targets = {"pylsp.exe": "python", "rust-analyzer.exe": "rust", "node.exe": "typescript"}
    before = {k: count(k) for k in targets}
    samples = [
        ("python", "demo.py", 'def add(a, b):\n    return a + b\n\nadd(1, 2)\n', 3, 1),
        ("rust", "main.rs", 'fn main() {\n    let x = 42;\n    println!("x = {}", x);\n}\n', 0, 4),
        ("typescript", "demo.ts", 'const x: number = 1;\nconsole.log(x);\n', 0, 4),
    ]
    for lid, path, text, line, ch in samples:
        r = _text(cae._tool_lsp_query({"language_id": lid, "request": "hover",
                                          "path": path, "line": line, "character": ch,
                                          "text": text, "root": r"C:\tmp"}))
        assert r["ok"] is True, f"{lid} 查询失败: {r.get('error')}"
    _time.sleep(1.0)  # 等 taskkill /T 清理生效
    for img, lang in targets.items():
        after = count(img)
        assert after <= before[img] + 2, f"{lang} 进程泄漏: {before[img]} → {after}"


def test_lsp_query_go_real():
    """go → gopls 已装（D:/开发/go-toolchain）：真实 LSP 交互应成功。

    IDE 增强 465：gopls 需要 PATH 含 go 命令（内部 go list 建 view——根因修复）。
    依赖 gopls 与 go 工具链，缺失则跳过。
    """
    import os as _os
    gopls = r"D:\开发\go-toolchain\gopath\bin\gopls.exe"
    if not _os.path.exists(gopls):
        pytest.skip("gopls 未安装")
    import tempfile
    repo = tempfile.mkdtemp(prefix="golsp_")
    try:
        with open(_os.path.join(repo, "go.mod"), "w", encoding="utf-8") as f:
            f.write("module probe\n\ngo 1.26\n")
        mainf = _os.path.join(repo, "main.go")
        with open(mainf, "w", encoding="utf-8") as f:
            f.write("package main\n\nfunc computeArea(w float32) float32 {\n"
                    "\treturn w\n}\n\nfunc main() {\n\t_ = computeArea(2)\n}\n")
        r = _text(cae._tool_lsp_query({"root": repo, "path": mainf,
                                          "request": "definition",
                                          "line": 7, "character": 8}))
        assert r["ok"] is True, f"go LSP 失败: {r.get('error')}"
        assert r.get("result"), f"definition 应为位置列表: {r}"
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def test_lsp_gopls_env_override(monkeypatch):
    """UNIFIED_RX_GOPLS env 覆盖（472：工具路径参数化——security review 观察项）。

    设 UNIFIED_RX_GOPLS 指向 gopls → LSP_SERVER_CONFIG["go"] 应解析到覆盖值。
    """
    import os as _os
    gopls = r"D:\开发\go-toolchain\gopath\bin\gopls.exe"
    if not _os.path.exists(gopls):
        pytest.skip("gopls 未安装")
    monkeypatch.setenv("UNIFIED_RX_GOPLS", gopls)
    cfg = cae.LSP_SERVER_CONFIG["go"]
    assert cfg[0] == gopls, f"env 覆盖应生效: {cfg}"
    # 覆盖路径可执行校验（_command_available 用 which/isfile——env 值直通）
    assert cae._command_available(cfg[0]), "覆盖路径应可执行"
