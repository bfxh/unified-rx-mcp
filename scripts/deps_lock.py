# -*- coding: utf-8 -*-
"""依赖红线机器化（S147）：`rust/Cargo.toml` 的依赖段**恒空**。

规范里这条写在迁移红线（[dependencies] 恒空——与 Python 侧纯 stdlib 同纪律），
但此前只有人读、没有门。本脚本解析 Cargo.toml 的依赖段，出现任何条目即红
（[dev-dependencies] / [build-dependencies] 同拦——保持零第三方）。

用法：python -X utf8 scripts/deps_lock.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARGO = ROOT / "rust" / "Cargo.toml"
SECTIONS = ("[dependencies]", "[dev-dependencies]", "[build-dependencies]",
            "[target.'cfg(unix)'.dependencies]")


def offenders(text):
    out, cur = [], None
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line
            continue
        if cur in SECTIONS and line and not line.startswith("#"):
            out.append(f"L{ln}: {cur} {line[:60]}")
    return out


def main():
    if not CARGO.is_file():
        sys.exit(f"DEPS-LOCK FAIL: 缺 {CARGO}")
    text = CARGO.read_text(encoding="utf-8")
    bad = offenders(text)
    print(f"DEPS-LOCK rust/Cargo.toml 依赖段条目={len(bad)}")
    for b in bad:
        print("  ", b)
    if bad:
        sys.exit("DEPS-LOCK FAIL: [dependencies] 恒空红线被破")
    print("DEPS-LOCK OK")


if __name__ == "__main__":
    main()
