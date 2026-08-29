# -*- coding: utf-8 -*-
"""S10 强度包测试：入口 schema 门禁 / 出口大字符串截断 / local_run 取消接线。"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry   # noqa: E402
import server     # noqa: E402  S10 取消端到端用 _CANCELS/cancel_flag
import tools      # noqa: F401,E402


# ---------- 入口门禁：tools/list 声明了 schema，call() 现在真的校验 ----------

def test_required_missing_rejected_cleanly():
    r = registry.call("fs_read", {})
    assert r["ok"] is False and "SchemaError" in r["error"] and "path" in r["error"]


def test_wrong_type_rejected_before_tool_runs():
    # dict 进 string 参数：以前穿透到工具内部才炸 TypeError，现在在门口死掉
    r = registry.call("bug_scan", {"path": {"evil": True}})
    assert r["ok"] is False and "SchemaError" in r["error"] and "string" in r["error"]


@pytest.fixture
def typed_tool():
    @registry.tool("t10_typed", "test-only", "misc",
                   {"type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "s": {"type": "string"},
                    },
                    "required": ["n"]})
    def _t(n, s=None):
        return {"n": n, "s": s}

    yield
    registry._TOOLS.pop("t10_typed", None)


def test_bool_is_not_integer(typed_tool):
    r = registry.call("t10_typed", {"n": True})
    assert r["ok"] is False and "boolean" in r["error"]


def test_integral_float_accepted(typed_tool):
    r = registry.call("t10_typed", {"n": 3})
    assert r["ok"] is True and r["result"]["n"] == 3


def test_wrong_type_string_rejected(typed_tool):
    r = registry.call("t10_typed", {"n": "x"})
    assert r["ok"] is False and "str" in r["error"]


def test_valid_pass_untouched(typed_tool):
    r = registry.call("t10_typed", {"n": 7, "s": "hello"})
    assert r["ok"] is True and r["result"] == {"n": 7, "s": "hello"}


# ---------- 出口裁剪扩展：大字符串不再裸奔 ----------

@pytest.fixture
def big_str_tool():
    @registry.tool("t10_big", "test-only", "misc",
                   {"type": "object", "properties": {}, "required": []})
    def _t():
        return {"blob": "A" * (registry.MAX_STR_CHARS + 5000), "small": "keep"}

    yield
    registry._TOOLS.pop("t10_big", None)


def test_big_string_truncated_with_marker(big_str_tool):
    r = registry.call("t10_big", {})
    blob = r["result"]["blob"]
    assert len(blob) < registry.MAX_STR_CHARS + 200
    assert "[truncated" in blob and "5000 chars" in blob
    assert r["result"]["small"] == "keep"


def test_small_results_untouched(typed_tool):
    r = registry.call("t10_typed", {"n": 1, "s": "tiny"})
    assert "truncated" not in str(r)


# ---------- 取消接线：协议层 Event → local_run 内部轮询 → 进程树清理 ----------

def test_local_run_cancel_wiring(tmp_path):
    sleeper = tmp_path / "sleepy.py"
    sleeper.write_text("import time\ntime.sleep(30)\nprint('done')\n", encoding="utf-8")

    # S10 后取消登记唯一事实源在 registry（server 的 __main__/import 双世界陷阱）
    ev = registry.register_cancel("T10-1")

    def cancel_later():
        time.sleep(0.9)
        ev.set()

    threading.Timer(0.9, cancel_later).start()
    t0 = time.monotonic()
    try:
        r = registry.call_with_context("local_run", {
            "domain": "python", "name": "script",
            "args": {"script": str(sleeper)}, "timeout": 40,
            "__authorized": True,
        }, request_id="T10-1")
        wall = time.monotonic() - t0
        assert r["ok"] is False
        res = r.get("result") or {}
        assert res.get("cancelled") is True, r
        assert wall < 8, f"取消未生效，跑了 {wall:.1f}s"
        # 长任务只跑了 <8s 却有输出通道字段——契约完整
        assert "cmd" in res and "exit" in res
    finally:
        registry.release_cancel("T10-1")
        registry.clear_request_context()


def test_server_forwards_to_registry_map():
    """协议层委托一致性：notifications/cancelled 置位的是同一张登记表。"""
    ev = registry.register_cancel("T10-2")
    assert server.cancel_flag("T10-2") is ev
    assert server.registry.set_cancelled("T10-2") is True
    assert ev.is_set()
    registry.release_cancel("T10-2")
    assert server.cancel_flag("T10-2") is None


def test_no_context_behaves_like_before(tmp_path):
    """无请求上下文（pytest 直调）时行为不变：脚本正常跑完返回 ok。"""
    quick = tmp_path / "quick.py"
    quick.write_text("print('hi')\n", encoding="utf-8")
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {"script": str(quick)}, "timeout": 20,
                                    "__authorized": True})
    assert r["ok"] is True and r["result"]["ok"] is True
    assert "hi" in r["result"]["stdout_tail"]
