# -*- coding: utf-8 -*-
"""S70：钳制保头保尾 + 类型符号（struct/enum/trait/impl/class）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from registry import _clamp  # noqa: E402
from tools.scan import _symbol_spans  # noqa: E402

RUST_SRC = """pub struct WheelCompound {
    pub center: Vec3,
    pub parts: Vec<(Vec3, Quat, Collider)>,
}

pub enum ClassifyMode {
    Category,
    Corp,
}

pub fn small_fn() -> u32 {
    1
}
"""


# ---------- 钳制保头保尾 ----------

def test_clamp_keeps_head_and_tail():
    """S70：>64KB 字符串钳制必须保尾——测试摘要/panic 消息都在尾部。"""
    big = "HEAD-" + ("x" * (registry.MAX_STR_CHARS + 5000)) + "-TAIL-SUMMARY"
    out = _clamp({"stdout": big}, {})
    s = out["stdout"]
    assert len(s) <= registry.MAX_STR_CHARS + 100
    assert s.startswith("HEAD-"), "头部丢失"
    assert "TAIL-SUMMARY" in s, "尾部丢失（摘要/panic 所在）"
    assert "truncated" in s


def test_clamp_short_string_untouched():
    out = _clamp({"stdout": "short"}, {})
    assert out["stdout"] == "short"


# ---------- 类型符号 ----------

def test_symbol_spans_rust_types_and_fns(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text(RUST_SRC, encoding="utf-8")
    spans = _symbol_spans((f.read_text(encoding="utf-8")).split("\n"), "rust")
    by_name = {n: (k, s + 1, e) for n, s, e, k in spans}
    assert by_name["WheelCompound"][0] == "type"
    assert by_name["ClassifyMode"][0] == "type"
    assert by_name["small_fn"][0] == "fn"
    # struct span 精确到自己的闭合括号（1-based；不吃掉后面的 fn）
    assert by_name["WheelCompound"][2] == 4, by_name["WheelCompound"]
    assert by_name["small_fn"][2] == 13


def test_symbol_spans_python_class(tmp_path):
    src = ("class Thing:\n"
           "    def method(self):\n"
           "        return 2\n"
           "\n"
           "def top_fn():\n"
           "    return 1\n")
    spans = _symbol_spans(src.split("\n"), "python")
    by_name = {n: (k, s + 1, e) for n, s, e, k in spans}
    assert by_name["Thing"][0] == "type"
    assert by_name["Thing"][1] == 1
    assert by_name["Thing"][2] == 3, by_name["Thing"]   # 1-based body 末行
    assert by_name["top_fn"][0] == "fn"


def test_outline_and_read_symbol_see_types(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text(RUST_SRC, encoding="utf-8")
    r = registry.call("ide_outline", {"file": str(f)})
    kinds = {s["name"]: s["kind"] for s in r["result"]["symbols"]}
    assert kinds.get("WheelCompound") == "type"
    assert kinds.get("small_fn") == "fn"
    r2 = registry.call("ide_read_symbol", {"file": str(f),
                                           "name": "WheelCompound"})
    assert r2["ok"], r2.get("error")
    assert r2["result"]["kind"] == "type"
    assert "parts" in r2["result"]["content"]
