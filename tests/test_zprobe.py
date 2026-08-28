# -*- coding: utf-8 -*-
import json
import sys


def test_probe(tmp_path):
    sys.path.insert(0, r'D:\开发\unified-rx-mcp\tests')
    from test_ide_build import call_tool
    (tmp_path / "crash.py").write_text("1/0\n", encoding="utf-8")
    r = call_tool("ide_debug", {"path": str(tmp_path),
                                "cmd": [sys.executable, "crash.py"]})
    print("\nPROBE:", json.dumps(r, ensure_ascii=False)[:400])
    assert True
