# -*- coding: utf-8 -*-
"""CI 秘密门禁（S124）：dogfood secrets_hunt，明文红线机器化。

红线：critical/high 级命中不得出现在测试区（tests/）之外。
测试区豁免的依据：夹具假值全部运行时拼接、按形状锁在 tmp_path（S123 纪律），
tests/ 里允许存在"碎片 + 合规假值"。
已知良性不拦（suspect/medium 本就不触发红线）：bench 语料、spec/ROUNDLOG.md 的
熵校验哈希、tools/*.py 字符白名单长串（熵层经典误报）。
历史提交不扫：GitHub push protection 在推送侧兜底新推送；改史治理见
spec/HARDENING.md（先吊销再清史）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
r = registry.call("secrets_hunt", {"path": root, "max_files": 8000,
                                   "max_results": 1000})
if not r.get("ok"):
    sys.exit(f"secrets_hunt 调用失败: {r.get('error')}")

res = r["result"]
bad = []
for h in res["hits"]:
    rel = str(h["file"]).replace("\\", "/")
    if rel == "tests" or rel.startswith("tests/") or "/tests/" in rel:
        continue
    if h["severity"] in ("critical", "high"):
        bad.append(h)

print(f"扫描 {res['files_scanned']} 文件 / 命中 {res['total_hits']} / "
      f"测试区外 critical+high {len(bad)}")
for h in res["hits"][:60]:
    print(f"  {h['severity']:8s} {h['rule']:20s} {h['file']}:{h['line']}")
if bad:
    sys.exit("秘密红线被触发（critical/high 出测试区）: " + "; ".join(
        f"{h['file']}:{h['line']} {h['rule']}" for h in bad))
print("SECRETS-GATE OK")
