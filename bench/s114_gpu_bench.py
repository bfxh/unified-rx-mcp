# -*- coding: utf-8 -*-
"""s114_gpu_bench.py —— GPU 内核 vs CPU 参考实现的交叉点实测（S114）。

口径：预热一次（内核编译/上下文只付一次），再按尺寸扫；每个尺寸取 3 次最小值。
输出：stdout 表 + 末行 JSON（留档 bench/results/s114_gpu.json）。
结论回填到 tools/gpu.py 的 CROSSOVER（auto 模式据此选路）。

用法：python bench/s114_gpu_bench.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools.gpu as gpu  # noqa: E402

SIZES_MB = (1, 2, 4, 8, 16, 32, 64)


def _best(fn, data, repeat=3):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(data)
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    return best, out


def main():
    st = gpu.status()
    rows = []
    if not st.get("available"):
        print("GPU 不可用:", st.get("reason"))
        print(json.dumps({"available": False, "reason": st.get("reason")}))
        return
    dev = st["platforms"][0]["devices"][0]
    print(f"GPU: {dev['name']} | CUs {dev['compute_units']} | VRAM {dev['vram_gb']}GB")

    # 预热（编译内核 + 建上下文）
    warm = b"x" * (1 << 20)
    gpu.byte_hist_gpu(warm)
    gpu.literal_scan_gpu(warm, [b"needle"])

    # 1) 字节直方图/熵
    print("\n-- byte_hist（直方图/熵）--")
    for mb in SIZES_MB:
        data = os.urandom(mb << 20)
        tg, hg = _best(gpu.byte_hist_gpu, data)
        tc, hc = _best(gpu.byte_hist_cpu, data)
        ok = hg == hc
        rows.append({"kind": "byte_hist", "mb": mb, "gpu_ms": round(tg, 2),
                     "cpu_ms": round(tc, 2), "speedup": round(tc / tg, 1), "equal": ok})
        print(f"  {mb:3d}MB  gpu {tg:8.2f}ms  cpu {tc:9.2f}ms  speedup {tc/tg:6.1f}x  equal={ok}")

    # 2) 字面量多模式匹配（CPU 用 bytes.find，是优化过的 memmem）
    print("\n-- literal_scan（多模式签名匹配，CPU 基线=bytes.find）--")
    pats = [f"SIG_{i:04d}".encode() for i in range(64)] + [b"EVIL_MARKER_42"]
    for mb in SIZES_MB:
        data = (b"benign payload " * (mb << 17)) + b"EVIL_MARKER_42"
        tg, gh = (_best(lambda d: gpu.literal_scan_gpu(d, pats), data, 1)
                  if mb <= 16 else (None, None))
        tc, ch = _best(lambda d: gpu.literal_scan_cpu(d, pats), data, 1)
        ok = (gh == ch) if gh is not None else None
        rows.append({"kind": "literal_scan", "mb": mb,
                     "gpu_ms": round(tg, 2) if tg else None,
                     "cpu_ms": round(tc, 2), "equal": ok})
        g = f"{tg:8.2f}ms" if tg else "   skip  "
        print(f"  {mb:3d}MB  gpu {g}  cpu {tc:9.2f}ms  equal={ok}")

    hist_rows = [r for r in rows if r["kind"] == "byte_hist"]
    cross = next((r["mb"] for r in hist_rows if r["speedup"] >= 1.0), None)
    print(f"\n直方图交叉点（GPU 首次快过 CPU）：{cross}MB" if cross else "\n直方图：本机未测到交叉点")
    out = {"available": True, "gpu": dev, "rows": rows, "hist_crossover_mb": cross,
           "note": "literal_scan 在 CPU(bytes.find/memmem) 下极难被朴素 GPU 内核超越——"
                   "auto 模式对字面量匹配保持 CPU，除非模式数×数据量远大于本测例"}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
