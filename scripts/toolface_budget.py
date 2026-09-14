# -*- coding: utf-8 -*-
"""工具面体量仪表（S143，兑现 EXTERNAL-ALIGNMENT A2）。

tools/list 是每个会话开场的**固定摊派**（宿主把全部工具定义塞进上下文）。
没有仪表时它只会无声膨胀——本脚本把体量变成可对账数字：
字符数 / CJK 比 / 估算 token / top 大户，并设**软上限**硬判（超帽即红）。

口径（与外部的对照基线）：
- 序列化用 ensure_ascii=False——与 server.py 发线上（ensure_ascii=False）
  同口径；解析后 CJK 按 ~1 token/字、ASCII 按 ~4 字符/token 估算。
- 对照：外部实测 58 工具 ≈ 55K token；本仓 S143 摸底 72 工具 ≈ 12.7K token。
- 软上限 = S143 实测 38,119 字符 + ~18% 余量（允许小步长薪，拦大幅膨胀）。
  要抬帽必须改这里并写明理由（改帽=记账动作，不许悄悄）。
- env `UNIFIED_RX_TOOLFACE_CAP` 可覆盖（调参/实验用；CI 用默认值）。

只读、零副作用；不写盘、不打点。
"""
import json
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

# 与 ci_secrets_gate 同纪律：自给自足声明沙盒（导入 tools 若触路径解析不依赖
# CI env；list_tools 本身不触盘，但防御性对齐先例）。
os.environ.setdefault("UNIFIED_RX_SANDBOX", root)

import registry  # noqa: E402
import tools  # noqa: E402,F401

# S143 摸底：38,119 字符 → 45,000 封顶（+18%）。抬帽需在 ROUNDLOG 写明理由。
_DEFAULT_CAP = 45000


def measure():
    ts = registry.list_tools()
    s = json.dumps(ts, ensure_ascii=False)
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    ascii_ = sum(1 for ch in s if ord(ch) < 128)
    est_tokens = round(cjk + ascii_ / 4)
    top = sorted(((len(json.dumps(t, ensure_ascii=False)), t["name"]) for t in ts),
                 reverse=True)[:5]
    return {"tools": len(ts), "total_chars": len(s), "cjk": cjk,
            "ascii": ascii_, "est_tokens": est_tokens, "top": top}


def main():
    cap = int(os.environ.get("UNIFIED_RX_TOOLFACE_CAP", _DEFAULT_CAP))
    m = measure()
    top_s = ", ".join(f"{n}:{c}" for c, n in m["top"])
    print(f"TOOLFACE tools={m['tools']} total_chars={m['total_chars']} "
          f"cjk={m['cjk']} ascii={m['ascii']} est_tokens={m['est_tokens']} "
          f"cap={cap} top5=[{top_s}]")
    if m["total_chars"] > cap:
        over = m["total_chars"] - cap
        sys.exit(f"TOOLFACE-GATE FAIL: 超帽 {over} 字符（{m['total_chars']} > {cap}）"
                 f"——先瘦身描述或（记账后）在 scripts/toolface_budget.py 抬帽；"
                 f"top5=[{top_s}]")
    print("TOOLFACE-GATE OK")


if __name__ == "__main__":
    main()
