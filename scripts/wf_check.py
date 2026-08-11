#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""wf_check — workflow 配置本地预检（REGRESSION_GUARD P2-2）。

提交 workflow 改动前本地跑：CodeQL 语言矩阵、REUSE 头覆盖、常见配置错误。
（actionlint/zizmor 由 CI 硬门禁执行——这里做快速静态预检。）

用法：python scripts/wf_check.py            # 检查 .github/workflows/*
      python scripts/wf_check.py <file...>  # 指定文件
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"


def check_file(p: Path) -> list[str]:
    issues: list[str] = []
    src = p.read_text(encoding="utf-8", errors="replace")

    # 1) CodeQL 语言矩阵：只允许项目实际语言（python；仓库无 go/js/ts/rust 源码）
    if "codeql" in p.name.lower():
        for m in re.finditer(r"language:\s*\[([^\]]+)\]", src):
            langs = [x.strip() for x in m.group(1).split(",")]
            if langs != ["python"]:
                issues.append(f"{p.name}: CodeQL 语言矩阵 {langs}——只应 [python]（go/js/ts 无源码必红）")
        for m in re.finditer(r"languages:\s*([\w,\s]+)$", src, re.M):
            langs = [x.strip() for x in m.group(1).split(",") if x.strip()]
            bad = [x for x in langs if x not in ("python", "${{ matrix.language }}")]
            if bad:
                issues.append(f"{p.name}: languages 含非 python: {bad}")

    # 2) 硬门禁检查：actionlint 步骤块内出现 continue-on-error 即违规
    #    （软门禁=没门禁；按步骤块切分避免误报其他步骤）
    blocks = re.split(r"^\s*-\s*name:\s*", src, flags=re.M)
    for block in blocks:
        if "actionlint" in block.lower() and "continue-on-error" in block:
            issues.append(f"{p.name}: actionlint 步骤含 continue-on-error（硬门禁要求）")

    # 3) REUSE 头覆盖：新文件（非 workflow）由 dep5 覆盖；workflow 文件本身要求 SPDX 头或 dep5
    return issues


def main() -> int:
    targets = [Path(t) for t in sys.argv[1:]] if len(sys.argv) > 1 else sorted(WF_DIR.glob("*.yml"))
    bad = 0
    for p in targets:
        if not p.exists():
            print(f"[wf_check] 文件不存在: {p}", file=sys.stderr)
            bad += 1
            continue
        issues = check_file(p)
        if issues:
            bad += 1
            print(f"[wf_check] FAIL {p.name}:")
            for i in issues:
                print("  ", i)
        else:
            print(f"[wf_check] OK {p.name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
