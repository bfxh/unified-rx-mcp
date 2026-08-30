# -*- coding: utf-8 -*-
"""S72：错误可修性（error_detail 堆栈尾部）+ 钳制嵌套递归 + local_run 解码/上限。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
import tools  # noqa: E402,F401
from registry import _clamp  # noqa: E402
from tools.meta import _run_decode, _run_tail, _run_caps  # noqa: E402

BIG = registry.MAX_STR_CHARS + 5000


# ---------- registry.call / server 错误带堆栈尾部 ----------

def _boom_entry(monkeypatch, tool="fs_stat"):
    def _boom(**kw):
        raise ValueError("boom-marker")
    entry = dict(registry._TOOLS[tool])
    entry["handler"] = _boom
    monkeypatch.setitem(registry._TOOLS, tool, entry)


def test_call_error_has_error_detail(monkeypatch):
    """S72：工具抛非 TypeError 异常时必须附 error_detail（堆栈尾部）。"""
    _boom_entry(monkeypatch)
    r = registry.call("fs_stat", {"path": __file__})
    assert r["ok"] is False
    assert "ValueError: boom-marker" in r["error"]
    assert "boom-marker" in r.get("error_detail", ""), "应附堆栈尾部"
    assert r["error_detail"].strip().splitlines()[-1].startswith("ValueError:")


def test_server_error_text_composes_detail(monkeypatch):
    """S72：协议层 ERROR 文本必须拼上 DETAIL，模型才看得出错位置。"""
    import server
    _boom_entry(monkeypatch)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "fs_stat", "arguments": {"path": __file__}}}
    resp = server._handle(msg)
    text = resp["result"]["content"][0]["text"]
    assert resp["result"]["isError"] is True
    assert text.startswith("ERROR:")
    assert "DETAIL:" in text and "boom-marker" in text


# ---------- 钳制：嵌套递归 + 全字段 ----------

def test_clamp_nested_dict_string_keeps_head_tail():
    """S72：嵌套在子 dict 里的超大 str 也要保头保尾（旧版直接漏网）。"""
    big = "NHEAD-" + ("y" * BIG) + "-NTAIL"
    out = _clamp({"result": {"detail": big}}, {})
    s = out["result"]["detail"]
    assert s.startswith("NHEAD-")
    assert "-NTAIL" in s
    assert "truncated" in s


def test_clamp_nested_list_truncated_with_markers():
    out = _clamp({"data": {"items": list(range(500))}}, {})
    items = out["data"]["items"]
    assert len(items) == registry.MAX_RESULT_ITEMS
    assert out["data"]["items_total_items"] == 500
    assert out["data"]["items_truncated"] is True
    assert items[0] == 0


def test_clamp_top_level_pagination_unchanged():
    """S10 契约保持：顶层 list 仍走 cursor 分页，末页不带 truncated。"""
    out = _clamp({"rows": list(range(500))}, {})
    assert len(out["rows"]) == 200
    assert out["truncated"] is True
    assert out["next_cursor"] == 200
    out2 = _clamp({"rows": list(range(500))}, {"cursor": 400})
    assert out2["rows"][0] == 400
    assert "truncated" not in out2


def test_clamp_multi_field_independent():
    """S72：旧版 break 只钳第一个超限字段；现在各字段独立处理。"""
    big = "z" * BIG
    out = _clamp({"a": big, "b": big}, {})
    assert "truncated" in out["a"]
    assert "truncated" in out["b"]


def test_clamp_depth_limit():
    """超过 _CLAMP_MAX_DEPTH 的深层不再扫（防递归开销），浅层正常。"""
    out = _clamp({"l1": {"l2": {"l3": {"l4": "ok"}}}}, {})
    assert out["l1"]["l2"]["l3"]["l4"] == "ok"
    huge = {"l1": {"l2": {"l3": {"l4": "x" * BIG}}}}   # 第 4 层，超深 → 原样
    out2 = _clamp(huge, {})
    assert len(out2["l1"]["l2"]["l3"]["l4"]) == BIG


# ---------- local_run 解码与错误感知上限 ----------

def test_run_decode_utf8_first_gbk_fallback():
    assert _run_decode("中文输出".encode("utf-8")) == "中文输出"
    assert _run_decode("中文输出".encode("gbk")) == "中文输出"
    assert isinstance(_run_decode(b"\xff\xfe\x00bad"), str)   # 永不抛异常


def test_run_caps_error_aware(monkeypatch):
    assert _run_caps(False) == (3000, 1000)
    fail_out, fail_err = _run_caps(True)
    assert fail_out >= 12000 and fail_err == 4000
    monkeypatch.setenv("UNIFIED_RX_RUN_TAIL_FAIL", "20000")
    assert _run_caps(True)[0] == 20000
    monkeypatch.setenv("UNIFIED_RX_RUN_TAIL_FAIL", "not-an-int")
    assert _run_caps(True)[0] == 12000


def test_run_tail_keeps_end():
    assert _run_tail("abc", 10) == "abc"
    assert _run_tail("abcdef", 3) == "def"


def test_local_run_failed_bigger_tail(tmp_path):
    """S72：失败时 stdout 尾巴放宽到 12000（旧版固定 3000），UTF-8 中文不乱码。"""
    script = tmp_path / "boom.py"
    script.write_text("print('E' * 40000)\nprint('错误原因: 编译失败')\nraise SystemExit(1)\n",
                      encoding="utf-8")
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {"script": str(script)}, "__authorized": True})
    assert r["ok"] is False
    res = r.get("result") or {}
    out = res.get("stdout_tail") or ""
    assert len(out) >= 10000, f"失败尾巴应放宽，实际 {len(out)}"
    assert "错误原因: 编译失败" in out, "UTF-8 输出不应乱码且关键行应在尾部"


def test_local_run_ok_keeps_small_tail(tmp_path):
    """成功时维持小尾巴（防上下文膨胀）。"""
    script = tmp_path / "fine.py"
    script.write_text("print('O' * 40000)\n", encoding="utf-8")
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {"script": str(script)}, "__authorized": True})
    assert r["ok"] is True
    assert len(r["result"]["stdout_tail"]) <= 3000
