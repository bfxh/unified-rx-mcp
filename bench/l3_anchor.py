# -*- coding: utf-8 -*-
"""L3 环境锚：VF3 cargo test 实跑 → bench/results/l3_env_anchor.json（--env 生成）。"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VF3 = r"D:\开发\VoxelForge-V3"
OUT = os.path.join(HERE, "results", "l3_env_anchor.json")


def main():
    if not os.path.isdir(os.path.join(VF3, ".git")):
        print("[SKIP] VoxelForge-V3 不在本地")
        return
    t0 = time.time()
    r = subprocess.run(["cargo", "test", "--quiet"], cwd=VF3, capture_output=True,
                       timeout=3600)
    out = ((r.stdout or b"") + (r.stderr or b"")).decode(errors="replace")
    passed = sum(int(m) for m in re.findall(r"test result: ok\. (\d+) passed", out))
    failed = sum(int(m) for m in re.findall(
        r"test result: FAILED\. (\d+) passed; (\d+) failed", out))
    anchor = {"cargo_test_ok": r.returncode == 0,
              "passed": passed, "failed": failed,
              "wall_s": round(time.time() - t0, 1),
              "tail": out[-500:]}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(anchor, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] passed={passed} failed={failed} "
          f"({anchor['wall_s']}s) -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
