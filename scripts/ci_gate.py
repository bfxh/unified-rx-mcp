# -*- coding: utf-8 -*-
"""CI 硬门禁（S124）：把 selftest 对账行从"提示"升为"退出码"。

selftest 本身对 VERSION_TAG/EXE_TAG 只打印不退出（开发期语义）。CI 必须严苛：
对账行不达标 = 本 job 红。DRIFT 视为漂移信号，SKIP（无 tag/exe 全缺）同样不放过
——CI 上必须 fetch-depth: 0 + cargo build，两个 SKIP 都是流程失守。
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

out = subprocess.run([sys.executable, "-X", "utf8", "server.py", "--selftest"],
                     capture_output=True, text=True, encoding="utf-8").stdout
print(out, end="")

fail = []
if "SCHEMA_BAD 0" not in out:
    fail.append("SCHEMA_BAD != 0")
if "SELFTEST tools=" not in out:
    fail.append("SELFTEST tools 行缺失（registry 没起来？）")

m = re.search(r"EXE_TAG ok=(\d+) drift=(\d+) missing=(\d+)", out)
if not m:
    fail.append("EXE_TAG 非 ok 行（SKIP=CI 未 cargo build，流程失守）")
elif m.group(2) != "0" or m.group(3) != "0" or int(m.group(1)) < 1:
    fail.append(f"EXE_TAG ok={m.group(1)} drift={m.group(2)} missing={m.group(3)}"
                "（drift/missing 必须 0）")

if not re.search(r"VERSION_TAG (OK|NEXT)", out):
    fail.append("VERSION_TAG DRIFT/SKIP（DRIFT=版本落后实锤；SKIP=checkout 未带 tag）")

if "SKILLS_DOCS stale=0 dead=0" not in out:
    fail.append("SKILLS_DOCS 有 stale/dead（文档账不平）")

if fail:
    sys.exit("硬门禁失败: " + "; ".join(fail))
print("CI-GATE OK")
