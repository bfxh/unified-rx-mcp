# -*- coding: utf-8 -*-
"""failure_analyze 测试（阶段2：RCA 根因链）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failure_analyze as fa  # noqa: E402


def test_parse_traceback():
    tb = 'Traceback (most recent call last):\n' \
         '  File "a/b.py", line 12, in run\n' \
         '    work()\n' \
         '  File "a/b.py", line 5, in work\n' \
         'ValueError: boom'
    d = fa._parse_traceback(tb)
    assert d["exception"] == "ValueError: boom"
    assert d["file"] == "a/b.py"
    assert d["line"] == 12
    assert len(d["frames"]) == 2


def test_parse_traceback_garbage():
    d = fa._parse_traceback("随便一段文字没有异常")
    assert d["exception"] == "随便一段文字没有异常"
    assert d["frames"] == []
    assert d["file"] == ""


def test_failure_analyze_basic(monkeypatch, tmp_path):
    """基本流程：异常解析 + 文件存在性验证 + 建议。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    # 真实存在的文件
    real = tmp_path / "real.py"
    real.write_text("x = 1\n", encoding="utf-8")
    tb = f'Traceback: File "{real}", line 1, in main\nTypeError: bad'
    r = fa.failure_analyze(tb, root=str(tmp_path))
    assert r["ok"] is True
    assert r["location"]["exists"] is True
    assert r["exception"] == "TypeError: bad"
    assert any("causal_trace" in s for s in r["suggestions"])


def test_failure_analyze_missing_file():
    """虚构文件 → 低置信候选（防幻觉）。"""
    tb = 'File "no/such.py", line 3, in f\nError: x'
    r = fa.failure_analyze(tb, root="")
    assert r["location"]["exists"] is False
    low = [c for c in r["candidates"] if c["confidence"] == "low"]
    assert any("不存在" in c["hypothesis"] for c in low)


def test_failure_analyze_telemetry_match(monkeypatch, tmp_path):
    """遥测同错误匹配 → rank=1 high 候选。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    import telemetry_core
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    telemetry_core.tick_tool("bug_scan", {"path": "x"}, 800.0, False,
                             "ValueError: boom in render")
    telemetry_core.flush()
    tb = 'File "x.py", line 1, in f\nValueError: boom in render'
    r = fa.failure_analyze(tb, root="")
    sources = [e["source"] for e in r["evidence"]]
    assert "telemetry_recent_errors" in sources
    top = r["candidates"][0]
    assert top["rank"] == 1 and top["confidence"] == "high"
    assert "遥测" in top["hypothesis"] or "工具调用" in top["hypothesis"]
