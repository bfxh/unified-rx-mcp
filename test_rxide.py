#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide（RX-IDE Lite 后端纯逻辑包）纯函数测试——只测逻辑，不碰网络/GUI。"""
import builtins
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rxide  # noqa: E402
from rxide import ai, commands, diff, settings, termlog  # noqa: E402


def test_version_and_root_path():
    assert rxide.__version__ == "0.1.0"
    # 包导入即把项目根插入 sys.path——存量模块直达
    import dashboard  # noqa: F401
    import ide_commands  # noqa: F401
    import ide_fusion  # noqa: F401
    assert callable(ide_commands.local_run)
    assert ide_fusion._FN_RE is not None
    assert callable(dashboard._read_jsonl)


# ── commands.parse ─────────────────────────────────────────
def test_parse_prefixes():
    assert commands.parse(">cargo build") == {"kind": "term", "body": "cargo build"}
    assert commands.parse("/explain 这段逻辑") == {"kind": "explain", "body": "这段逻辑"}
    assert commands.parse("/fix 编译报错") == {"kind": "fix", "body": "编译报错"}
    assert commands.parse("帮我改下 x") == {"kind": "edit", "body": "帮我改下 x"}


def test_parse_word_boundary():
    # 斜杠命令按首 token 精确匹配（词边界）——前缀近似词不误判
    assert commands.parse("/explains foo") == {"kind": "edit", "body": "/explains foo"}
    assert commands.parse("/fixit now") == {"kind": "edit", "body": "/fixit now"}
    assert commands.parse("/fix") == {"kind": "fix", "body": ""}
    assert commands.parse("/explain") == {"kind": "explain", "body": ""}


# ── commands.build_context ─────────────────────────────────
def test_build_context_window():
    text = "\n".join(f"line{i} = {i}" for i in range(1, 101))
    r = commands.build_context(text, 50)  # 无函数 → 光标 ±20 行
    assert r["line_count"] == 41
    assert r["context_text"].startswith("line30")
    assert r["context_text"].endswith("line70 = 70")
    assert r["fn_name"] is None


def test_build_context_full():
    text = "\n".join(f"l{i}" for i in range(60))
    r = commands.build_context(text, 3, full=True)
    assert r["context_text"] == text
    assert r["line_count"] == 60


def test_build_context_fn_locate():
    text = "\n".join([
        "import os", "",
        "def alpha():", "    a = 1", "    return a", "",
        "def beta():", "    b = 2", "    return b",
    ])
    r = commands.build_context(text, 8)  # 光标在 beta 内
    assert r["fn_name"] == "beta"
    assert r["context_text"].startswith("def beta():")
    assert "b = 2" in r["context_text"]
    assert "alpha" not in r["context_text"]


def test_build_context_nested_fn():
    # 内层函数估计尾未覆盖光标 → 继续向上定位外层函数
    text = "\n".join([
        "def outer():",
        "    def inner():",
        "        return 1",
        "    x = inner()",
        "    return x",
    ])
    r = commands.build_context(text, 4)  # 光标在 inner 之后（x = inner()）
    assert r["fn_name"] == "outer"
    assert r["context_text"].startswith("def outer():")
    assert "return x" in r["context_text"]


def test_build_context_multiline_signature():
    # Python 多行签名：括号归零后函数体缩进仍深于基准 → 不按括号语言截断
    text = "\n".join([
        "def calc(a,",
        "         b):",
        "    c = a + b",
        "    return c",
    ])
    r = commands.build_context(text, 3)
    assert r["fn_name"] == "calc"
    assert r["context_text"] == text


def test_estimate_fn_end_string_brace():
    # 字符串里的括号不参与平衡（防 printf("}") 提前归零截断）
    lines = ['int f() {', '    printf("}");', '    int a = 1;', '}']
    assert commands._estimate_fn_end(lines, 0) == 3


def test_build_context_selection():
    text = "\n".join(f"s{i}" for i in range(1, 101))
    sel = {"start": 40, "end": 45, "text": ""}
    r = commands.build_context(text, 42, selection=sel)  # 选区 ±20
    assert r["context_text"].startswith("s20")
    assert r["context_text"].endswith("s65")
    assert r["line_count"] == 46


# ── commands.parse_llm_edit ────────────────────────────────
def test_parse_llm_edit_fence():
    reply = "前言\n```python\na = 1\n```\n中间\n```rust\nlet b = 2;\n```\n后记"
    assert commands.parse_llm_edit(reply) == "let b = 2;"  # 取最后一个围栏


def test_parse_llm_edit_none():
    assert commands.parse_llm_edit("没有代码块的纯文本") is None


def test_parse_llm_edit_no_server(monkeypatch):
    # 评审修复：server 解析通道已完全移除——纯围栏提取，不得 import server
    real_import = builtins.__import__

    def _guard(name, *args, **kwargs):
        assert name != "server", "parse_llm_edit 不得 import server"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    assert commands.parse_llm_edit("```py\nx = 1\n```") == "x = 1"
    assert commands.parse_llm_edit("纯文本") is None


# ── diff.apply_edit ────────────────────────────────────────
def test_apply_edit_replace():
    new, s, e = diff.apply_edit("l1\nl2\nl3\nl4\n", "X\nY",
                                selection={"start": 2, "end": 3, "text": ""})
    assert new == "l1\nX\nY\nl4\n"
    assert (s, e) == (2, 3)


def test_apply_edit_insert():
    new, s, e = diff.apply_edit("l1\nl2", "N1\nN2", cursor_line=1)
    assert new == "l1\nN1\nN2\nl2"
    assert (s, e) == (2, 3)


# ── diff.line_diff ─────────────────────────────────────────
def test_line_diff_stats_and_previews():
    d = diff.line_diff("a\nb\nc\nd\n", "a\nX\nc\nd\nE\n")
    assert d["added"] == [2, 5]
    assert d["removed"] == [{"after_line": 1, "content": "b"}]
    assert d["stats"] == {"add": 2, "del": 1}
    assert d["previews"][0] == {"line": 2, "before": ["b"], "after": ["X"]}
    assert d["previews"][-1]["after"] == ["E"]


def test_line_diff_identical():
    d = diff.line_diff("same\n", "same\n")
    assert d["added"] == [] and d["removed"] == []
    assert d["stats"] == {"add": 0, "del": 0} and d["previews"] == []


def test_line_diff_delete_first_line_clamp():
    # 删首行：previews.line 钳位 ≥1（不得出现 0）
    d = diff.line_diff("A\nB\n", "B\n")
    assert d["removed"] == [{"after_line": 0, "content": "A"}]
    assert d["stats"] == {"add": 0, "del": 1}
    assert d["previews"][0]["line"] == 1
    assert all(p["line"] >= 1 for p in d["previews"])


# ── settings ───────────────────────────────────────────────
def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_FILE", str(tmp_path / "rxide.json"))
    assert settings.load() == settings.DEFAULTS  # 无文件 → 默认
    out = settings.save({"model": "rx-mini", "font_size": 15, "evil_key": 1})
    assert out["model"] == "rx-mini" and out["font_size"] == 15
    assert "evil_key" not in out  # 非 DEFAULTS 键拒收
    assert settings.load()["model"] == "rx-mini"  # 落盘可回读


def test_settings_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "rxide.json"
    p.write_text("{坏的 JSON", encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_FILE", str(p))
    assert settings.load() == settings.DEFAULTS


def test_settings_masked():
    assert settings.masked({"api_key": ""})["api_key"] == ""
    assert settings.masked({"api_key": "abc"})["api_key"] == "****"
    assert settings.masked({"api_key": "sk-1234567890"})["api_key"] == "****7890"


def test_settings_save_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_FILE", str(tmp_path / "rxide.json"))
    settings.save({"model": "m1"})
    assert not os.path.exists(str(tmp_path / "rxide.json") + ".tmp")  # replace 后无残留
    # 落盘在临时文件阶段失败 → 旧配置不撕裂（到不了 replace）
    def _boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(settings.json, "dump", _boom)
    settings.save({"model": "m2"})
    monkeypatch.setattr(settings.json, "dump", json.dump)  # 手动复原（undo 会连 DATA_FILE 一起回退）
    assert settings.load()["model"] == "m1"


def test_settings_save_mask_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_FILE", str(tmp_path / "rxide.json"))
    settings.save({"api_key": "sk-real-1234"})
    out = settings.save({"api_key": "****1234"})  # 掩码回写 → 服务端忽略
    assert out["api_key"] == "sk-real-1234"
    assert settings.load()["api_key"] == "sk-real-1234"
    settings.save({"api_key": "sk-new-9999"})  # 真实 Key 正常写入不受影响
    assert settings.load()["api_key"] == "sk-new-9999"


# ── ai.parse_sse_line ──────────────────────────────────────
def test_parse_sse_line():
    obj = ai.parse_sse_line('data: {"choices":[{"delta":{"content":"hi"}}]}')
    assert obj == {"choices": [{"delta": {"content": "hi"}}]}
    assert ai.parse_sse_line("data: [DONE]") is None
    assert ai.parse_sse_line("") is None
    assert ai.parse_sse_line("event: ping") is None
    assert ai.parse_sse_line("data: {坏 json") is None


def test_chat_no_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_FILE", str(tmp_path / "none.json"))
    err = "未配置 API Key（右上角齿轮设置）"
    assert ai.chat([{"role": "user", "content": "hi"}]) == {"ok": False, "error": err}
    assert list(ai.chat([{"role": "user", "content": "hi"}], stream=True)) == \
        [{"type": "error", "error": err}]


# ── termlog ────────────────────────────────────────────────
def test_run_command_match():
    domain, entry, rest = termlog._match("cargo test")
    assert domain == "cargo" and entry["name"] == "test" and rest == []
    d2, e2, _ = termlog._match("clippy")  # 单 token 跨域按 name
    assert d2 == "cargo" and e2["name"] == "clippy"
    assert termlog._match("根本没有这命令") is None


def test_run_command_unknown():
    r = termlog.run_command("根本没有这命令")
    assert r["ok"] is False and r["error"] == "未知命令"
    assert len(r["available"]) == 20
    assert r["available"][0] == "cargo/build"


def test_log_tail(tmp_path, monkeypatch):
    p = tmp_path / "scan-log.jsonl"
    recs = [{"ts": f"2026-08-16 10:00:0{i}", "tool": "bug_scan",
             "ok": i % 2 == 0, "summary": f"扫描 {i}"} for i in range(5)]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                 encoding="utf-8")
    monkeypatch.setattr(termlog, "LOG_FILE", str(p))
    r1 = termlog.log_tail(0)
    assert r1["cursor"] == 5 and len(r1["lines"]) == 5
    assert r1["lines"][0].startswith("2026-08-16 10:00:00")
    assert "bug_scan" in r1["lines"][0]
    assert termlog.log_tail(r1["cursor"]) == {"lines": [], "cursor": 5}
    r3 = termlog.log_tail(3)  # 增量：只拿第 4/5 条
    assert len(r3["lines"]) == 2
    assert r3["lines"][0].startswith("2026-08-16 10:00:03")


def test_log_tail_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(termlog, "LOG_FILE", str(tmp_path / "不存在.jsonl"))
    assert termlog.log_tail(0) == {"lines": [], "cursor": 0}


def test_log_tail_bigfile(tmp_path, monkeypatch):
    # >64KB 走 dashboard._read_jsonl 回读路径（块边界不丢条）
    p = tmp_path / "big.jsonl"
    recs = [{"ts": f"2026-08-16 10:{i // 60:02d}:{i % 60:02d}", "tool": "bug_scan",
             "ok": True, "summary": "x" * 60 + f" {i}"} for i in range(900)]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                 encoding="utf-8")
    assert p.stat().st_size > 65536
    monkeypatch.setattr(termlog, "LOG_FILE", str(p))
    r = termlog.log_tail(800)
    assert r["cursor"] == 900 and len(r["lines"]) == 100
    assert r["lines"][0].endswith("800") and r["lines"][-1].endswith("899")

def test_run_command_slash_form():
    # 斜杠写法与空格写法等价（available 列表以 domain/name 展示——照抄必中）
    d1, e1, r1 = termlog._match("git/status")
    d2, e2, r2 = termlog._match("git status")
    assert (d1, e1["name"]) == ("git", "status")
    assert (d1, e1) == (d2, e2) and r1 == r2 == []
    # 斜杠 + 额外参数：剩余 token 留给占位符填充
    d3, e3, r3 = termlog._match("cargo/test_one unified-rx test_a")
    assert d3 == "cargo" and e3["name"] == "test_one"
    assert r3 == ["unified-rx", "test_a"]
    assert termlog._match("不存在域/不存在名") is None

