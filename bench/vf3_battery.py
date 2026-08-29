# -*- coding: utf-8 -*-
"""VF3 battery: run full unified-rx tool surface over VoxelForge-V3, emit baseline JSON."""
import collections
import json
import sys
import time

sys.path.insert(0, ".")
import registry  # noqa: E402
import tools      # noqa: F401,E402

ROOT = r"D:\开发\VoxelForge-V3"
report = {"root": ROOT, "ts": int(time.time())}

t0 = time.time()
r = registry.call("bug_scan", {"path": ROOT, "max_files": 600})["result"]
report["bug_scan"] = {
    "files": r["files"], "total": r["total"],
    "by_severity": r["by_severity"], "by_rule": r["by_rule"],
    "elapsed_s": round(time.time() - t0, 2),
}

t0 = time.time()
r = registry.call("std_check", {"path": ROOT, "max_files": 600})["result"]
report["std_check"] = {
    "files": r["files"], "total": r["total"],
    "by_rule": dict(collections.Counter(f["rule"] for f in r["findings"])),
    "elapsed_s": round(time.time() - t0, 2),
}

t0 = time.time()
r = registry.call("ast_scan", {"path": ROOT, "max_files": 600})["result"]
rules = collections.Counter(i["rule"] for i in r["issues"])
langs = collections.Counter(u["lang"] for u in r["units"])
report["ast_scan"] = {
    "files": r["files"], "total": r["total"], "by_rule": dict(rules),
    "by_lang": dict(langs), "elapsed_s": round(time.time() - t0, 2),
    "rust_units": [u for u in r["units"] if u["lang"] == "rust"][:60],
    "rust_reach": (r.get("rust_reach") or {}).get("by_reach"),
    "test_only_helpers": (r.get("rust_reach") or {}).get("test_only_helpers", [])[:20],
}

r = registry.call("path_probe", {})["result"]
report["attack_path_probe"] = {"probes": r["probes"], "all_safe": r["all_safe"]}

r = registry.call("big_input", {"tool_name": "code_search",
                                "base_args": {"query": "damage", "root": ROOT},
                                "fuzz_field": "query"})["result"]
report["attack_big_input"] = {"all_pass": r["all_pass"], "cases": r["cases"]}

r = registry.call("input_fuzz", {"tool_name": "locate_edit",
                                 "base_args": {"path": ROOT, "query": "unwrap"},
                                 "fuzz_field": "query"})["result"]
report["attack_fuzz_locate"] = {"cases": r["cases"], "failures": r["failures"],
                                "noise": [c for c in r["results"] if c["verdict"] == "FAIL-noise"]}

r = registry.call("engine_query", {"query": "load_module_defs", "root": ROOT})["result"]
report["engine_query"] = {"engine": r.get("engine"), "total": r.get("total")}

print(json.dumps(report, ensure_ascii=False, indent=1))
with open(r"bench/vf3_baseline.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
