# -*- coding: utf-8 -*-
import json


def test_probe_build(tmp_path):
    import sys
    sys.path.insert(0, r'D:\开发\unified-rx-mcp\tests')
    sys.path.insert(0, r'D:\开发\unified-rx-mcp\bench')
    sys.path.insert(0, r'D:\开发\unified-rx-mcp')
    import test_s33_lang as m
    print("\ncall_tool module:", m.__name__, "| src file:", m.__file__)
    import inspect
    print("call_tool src:", inspect.getsource(m.call_tool))
    from test_s33_lang import call_tool
    (tmp_path / "Bad.java").write_text(
        "public class Bad { void f() { int x = \"s\"; } }", encoding="utf-8")
    raw = m.registry.call("ide_build", {"path": str(tmp_path)})
    print("RAW keys:", sorted(raw.keys()), "| raw ok:", raw.get("ok"))
    r = call_tool("ide_build", {"path": str(tmp_path)})
    print("MERGED keys:", sorted(r.keys()), "| merged ok:", r.get("ok"))
