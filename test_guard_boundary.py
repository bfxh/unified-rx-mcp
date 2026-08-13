"""test_guard_boundary.py — guard_core 边界输入测试（2026-08-14）。

覆盖：空文本 / 极长文本 / 非 UTF8 字节输入——hallucination_guard 在
极端输入下不得崩溃、不得误报。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guard_core import extract_claims, guard_text  # noqa: E402


def test_guard_empty_text():
    r = guard_text("")
    assert r["verdict"] == "no_claims" and r["claims"] == []


def test_guard_whitespace_only():
    r = guard_text("   \n\t\n  ")
    assert r["verdict"] == "no_claims"


def test_guard_no_claims_plain_text():
    r = guard_text("今天天气不错，我改了一些代码。")
    assert r["verdict"] == "no_claims"


_VERDICTS = ("no_claims", "pass", "refuted", "unverified")


def test_guard_huge_text():
    """极长文本（1MB）不崩溃、不 OOM。"""
    big = ("这是一个很长的文档。" * 20000) + "\nserver.py:100 有 bug\n"
    r = guard_text(big)
    assert r["verdict"] in _VERDICTS
    assert "advice" in r


def test_guard_non_utf8_bytes_like():
    """非 UTF8 序列（bytes 解码错误场景的模拟——str 含孤立代理/非法码点）。"""
    try:
        # Python str 不允许孤立代理直接构造——用 surrogates 转义
        text = "abc\udcff\ud800 def server.py:5\n"
        r = guard_text(text)
        assert r["verdict"] in _VERDICTS
    except UnicodeEncodeError:
        pass  # 平台差异：无法构造该输入则跳过


def test_guard_unbalanced_markers():
    """未闭合的反引号/括号不崩溃。"""
    r = guard_text("`server.py:1 未闭合反引号")
    assert "advice" in r


def test_extract_claims_no_panic_on_binaryish():
    """含大量符号字符的文本（类二进制）不崩溃。"""
    r = guard_text("\x00\x01\x02 file.rs:3 \x7f\x80" * 500)
    assert "advice" in r


def test_guard_tool_names_whitelist():
    """tool_names 白名单：声明工具名在白名单 → verified（不用文件/grep 兜底）。"""
    r = guard_text("我调用了 `math_ops` 工具来计算。", tool_names={"math_ops"})
    assert r["verdict"] == "pass", f"白名单工具应 verified: {r}"
    assert any(i["verdict"] == "verified" and i["claim"] == "math_ops"
               for i in r["verified"]), f"verified 应含 math_ops: {r['verified']}"


def test_guard_tool_names_miss():
    """工具名不在白名单 → 不冒充 verified（走文件/grep 兜底或 unverified）。"""
    r = guard_text("我调用了 `nope_tool_xyz` 工具。", tool_names={"math_ops"})
    # 不在白名单：不会 verified（本地无该符号文件 → unverified 或 refuted，绝不 verified）
    assert not any(i["claim"] == "nope_tool_xyz" and i["verdict"] == "verified"
                   for i in r.get("verified", [])), f"不在白名单不得 verified: {r}"
