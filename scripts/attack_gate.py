"""CI 自攻门（S138）：dogfood `attack_cruise`——全攻击面巡航 verdict 必须 clean。

覆盖：授权门自审（含组合透传静态检查）+ 路径探针 8 形态 + 四靶模糊
（input_fuzz×big_input）× 本包自身。任一 failures/errors 即 exit 1。
纪律同 secrets gate：沙盒自给自足声明（不依赖 workflow 注入 env）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("UNIFIED_RX_SANDBOX", ROOT)  # 与 ci_secrets_gate.py 同纪律

import registry  # noqa: E402
import tools  # noqa: E402,F401

r = registry.call("attack_cruise", {})
if not r.get("ok"):
    sys.exit(f"attack_cruise 调用失败: {r.get('error')}")

res = r["result"]
print(f"巡航 verdict={res['verdict']} fuzz={len(res['fuzz'])} "
      f"big={len(res['big'])} gates_ok={(res.get('gates') or {}).get('ok')} "
      f"passive_safe={(res.get('passive') or {}).get('all_safe')}")
for f in res.get("failures") or []:
    print("  FAIL", f)
for e in res.get("errors") or []:
    print("  ERR ", e)
if res["verdict"] != "clean":
    sys.exit("自攻门未过（failures/errors 见上）")
print("ATTACK-GATE OK")
