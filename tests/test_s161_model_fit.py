# -*- coding: utf-8 -*-
"""S161：模型适配 wire 层三件的**回归锁**（F1 一形态 / F2 structuredContent / F3 溢出落盘）。

为什么单列测试：这三件都是"对模型可见的契约"，最容易在后续重构里被悄悄改回去——
散文错误少有人注意、structuredContent 少一行不报错、溢出阈值一变就退回"截断丢信息"。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from tools import spill  # noqa: E402


def test_error_reply_is_json_with_next(monkeypatch):
    """F1：失败回包必须是 JSON（禁散文 `ERROR:`），且带下一步动作。"""
    resp = server.tool_reply(1, "fs_read", {"ok": False, "error": "参数 path 必填"})
    res = resp["result"]
    assert res["isError"] is True
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False
    assert "path" in payload["error"]["message"]
    assert payload["error"]["next"], "必须给下一步（否则弱模型只能盲重试）"
    assert not res["content"][0]["text"].startswith("ERROR:"), "散文错误已废止（S161）"


def test_structured_content_is_uniform(monkeypatch):
    """F2：机器可读那一层统一——成功 `{ok:true,data}`、失败 `{ok:false,error}`。"""
    ok = server.tool_reply(1, "sys_topology", {"ok": True, "result": {"vendor": "Intel"}})
    sc = ok["result"]["structuredContent"]
    assert sc["ok"] is True and sc["data"]["vendor"] == "Intel"
    assert "untrusted" not in json.dumps(sc)[:40], "可信工具不该带 trust 标记"
    bad = server.tool_reply(2, "sys_topology", {"ok": False, "error": "x"})
    assert bad["result"]["structuredContent"]["ok"] is False


def test_untrusted_is_a_field_not_only_prefix():
    """F6：不可信工具既加前缀（旧消费方）也在 structuredContent 里带 trust 字段（摘抄不丢）。"""
    resp = server.tool_reply(1, "fs_read", {"ok": True, "result": {"content": "x"}})
    text = resp["result"]["content"][0]["text"]
    assert text.startswith("[untrusted-content")
    sc = resp["result"]["structuredContent"]
    assert sc["trust"] == "untrusted" and sc["source"] == "fs_read"


def test_spill_writes_inside_sandbox_and_sanitizes_name(monkeypatch, tmp_path):
    """F3：溢出落盘只在沙盒内、文件名走白名单（恶意前缀不可能穿越）。"""
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    path = spill.spill("PAYLOAD", "../../etc/passwd")
    assert path and os.path.abspath(path).startswith(os.path.abspath(str(tmp_path)))
    assert os.path.basename(path).startswith("etcpasswd"), "白名单应剥掉分隔符与 `..`"
    assert open(path, encoding="utf-8").read() == "PAYLOAD"
    monkeypatch.delenv("UNIFIED_RX_SANDBOX", raising=False)
    assert spill.spill("x", "t") is None, "无沙盒 ⇒ 不落盘（fail-closed，不猜路径）"


def test_big_reply_spills_instead_of_inlining(monkeypatch, tmp_path):
    """F3：超阈值回包 ⇒ 摘要+路径（不再把大 JSON 塞进上下文），且**不再截断丢信息**。"""
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SPILL_KB", "1")
    big = {"rows": ["x" * 200] * 100}
    resp = server.tool_reply(1, "sys_topology", {"ok": True, "result": big})
    sc = resp["result"]["structuredContent"]
    assert "spilled" in sc and sc["spilled"]["path"]
    assert "fs_read" in sc["spilled"]["fetch"], "必须给出取用命令"
    assert len(resp["result"]["content"][0]["text"]) < 2000, "文本块只留摘要"
    on_disk = json.loads(open(sc["spilled"]["path"], encoding="utf-8").read())
    assert on_disk["data"] == big, "落盘的是**完整**结果（截断已降为最后手段）"
    monkeypatch.setenv("UNIFIED_RX_SPILL_KB", "0")
    resp2 = server.tool_reply(1, "sys_topology", {"ok": True, "result": big})
    assert "spilled" not in resp2["result"]["structuredContent"], "=0 关闭溢出"
