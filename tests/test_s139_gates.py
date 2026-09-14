# -*- coding: utf-8 -*-
"""S139 契约：数据流门（taint 基线）。

- 门判定必须绿（产品面 definite 对照 spec/taint-baseline.json）；
- 基线文件形状锁：entries 含 file/sink/count 且 **why 无占位残留**（人工确认过的
  账才准在册）；policy 说明排除面。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def test_taint_gate_runs_clean():
    cp = subprocess.run([sys.executable, "-X", "utf8",
                         os.path.join(ROOT, "scripts", "taint_gate.py")],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=600, cwd=ROOT)
    assert cp.returncode == 0, f"{cp.stdout[-500:]}\n{cp.stderr[-500:]}"
    assert "TAINT-GATE OK" in cp.stdout
    assert "bench/ 已排除" in cp.stdout


def test_taint_baseline_shape_and_whys_filled():
    with open(os.path.join(ROOT, "spec", "taint-baseline.json"), encoding="utf-8") as f:
        doc = json.load(f)
    assert "bench" in doc["policy"], doc["policy"]
    assert doc["entries"], "基线不得为空"
    for e in doc["entries"]:
        for k in ("file", "sink", "count", "why"):
            assert k in e, e
        assert int(e["count"]) >= 1, e
        assert "<请填写" not in e["why"], f"占位未填不得入库: {e}"
        assert len(e["why"]) >= 10, f"why 过短=未人工确认: {e}"
