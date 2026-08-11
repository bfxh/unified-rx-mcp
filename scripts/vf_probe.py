#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""VoxelForge-Nexus 实测 unified-rx 工具（ui_check/cb_index/cb_scan/ds_check/code_complete）。"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace\mcp-servers\unified-rx")
VF = r"D:\开发\VoxelForge-Nexus"
sys.path.insert(0, str(ROOT))
os.environ["UNIFIED_RX_SANDBOX"] = ""
import server

report = {}

# 1) ui_check：Bevy UI 静态检查（nexus_app 的 UI 源码）
t0 = time.perf_counter()
r = server._call("ui_check", {"path": os.path.join(VF, "crates", "nexus_app"), "max_files": 100})
dt = (time.perf_counter() - t0) * 1000
try:
    d = json.loads(r[0].text)
    report["ui_check"] = {"files": d.get("files", 0), "issues": len(d.get("issues", [])), "ms": round(dt, 0)}
    if d.get("issues"):
        report["ui_check"]["sample"] = [i.get("rule") for i in d["issues"][:5]]
except Exception as e:
    report["ui_check"] = {"error": str(e)[:120], "ms": round(dt, 0)}

# 2) cb_index：代码库索引（全库符号）
t0 = time.perf_counter()
r = server._call("cb_index", {"path": VF})
dt = (time.perf_counter() - t0) * 1000
try:
    d = json.loads(r[0].text)
    report["cb_index"] = {"files": d.get("file_count", 0), "symbols": d.get("symbol_count", 0), "ms": round(dt, 0)}
except Exception as e:
    report["cb_index"] = {"error": str(e)[:120], "ms": round(dt, 0)}

# 3) ds_check：设计系统合规（.rs 硬编码扫描）
t0 = time.perf_counter()
r = server._call("ds_check", {"path": os.path.join(VF, "crates", "nexus_app"), "max_files": 50})
dt = (time.perf_counter() - t0) * 1000
try:
    d = json.loads(r[0].text)
    report["ds_check"] = {"issues": len(d.get("issues", [])), "ms": round(dt, 0)}
except Exception as e:
    report["ds_check"] = {"error": str(e)[:120], "ms": round(dt, 0)}

# 4) code_complete：LSP rust 补全（nexus_app 某 rs 文件）
cand = None
for p in Path(VF, "crates", "nexus_app", "src").rglob("*.rs"):
    cand = str(p)
    break
if cand:
    t0 = time.perf_counter()
    src_lines = Path(cand).read_text(encoding="utf-8", errors="replace").splitlines()
    mid = max(0, len(src_lines) // 2)
    r = server._call("code_complete", {"path": cand, "line": mid, "character": 0, "timeout": 90})
    dt = (time.perf_counter() - t0) * 1000
    txt = r[0].text
    try:
        d = json.loads(txt)
        report["code_complete"] = {"file": Path(cand).name, "candidates": len(d.get("items", [])), "ms": round(dt, 0)}
    except Exception:
        report["code_complete"] = {"raw": txt[:80], "ms": round(dt, 0)}
else:
    report["code_complete"] = {"error": "no rs file"}

print(json.dumps(report, ensure_ascii=False, indent=2))
