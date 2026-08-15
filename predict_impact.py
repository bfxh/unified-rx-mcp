#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""predict_impact —— 预知引擎（2026-08-15，阶段2）。

"你要改 X，预测会破坏 Y"——改前预测：
① 影响面预测：引用图（ide_references 词级）→ 调用方文件/行
② 风险教训：lse 教训库匹配（同类符号/模式的历史坑）
③ 规则提示：game_check/std 红线（该文件可能踩的规则）

与 impact_via_references（改后确认）互补：改前预测 vs 改后校验。
全部只读——预测不执行任何写操作。
"""
import json
import os


def predict_impact(root: str, symbol: str,
                   file_hint: str = "") -> dict:
    """改前预测：改 symbol（可选限定 file_hint）会影响的文件 + 风险。"""
    from ide_tools import ide_references
    r = ide_references(root, symbol)
    if not r.get("ok"):
        return {"ok": False, "symbol": symbol,
                "error": r.get("error", "ide_references 失败")}
    refs = r.get("definitions", []) + r.get("references", [])
    # ① 影响面预测：引用文件（改前影响面 = 定义 + 全部引用所在文件）
    files: dict[str, int] = {}
    for ref in refs:
        fp = ref.get("file", "")
        if fp:
            files[fp] = files.get(fp, 0) + 1
    if file_hint and file_hint not in files:
        files[file_hint] = files.get(file_hint, 0)  # 显式限定文件
    # ② 风险教训：lse 教训库匹配（符号名/文件名的关键词）
    lessons: list[dict] = []
    try:
        import lse_client as _lse
        for kw in (symbol[:16], os.path.splitext(os.path.basename(file_hint))[0]
                   if file_hint else ""):
            if not kw or len(kw) < 2:
                continue
            try:
                rr = _lse.lesson_recall("work_" + _sha(kw))
                if isinstance(rr, dict) and rr.get("ok"):
                    item = rr.get("result") or {}
                    lessons.append({"keyword": kw,
                                    "content": str(item.get("content", ""))[:120]})
            except Exception:  # 尽力而为（教训库不可用不影响预测）
                pass
    except Exception:  # 尽力而为
        pass
    # ③ 规则提示：目标文件可能踩的规则（game_check/std 红线——静态关键词）
    rule_hints: list[str] = []
    for fp in list(files)[:5]:
        try:
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                src = f.read(65536)
            if "unwrap()" in src or ".expect(" in src:
                rule_hints.append(f"{os.path.basename(fp)}: 裸 unwrap/expect（panic 风险）")
            if "while True" in src or "loop {" in src:
                rule_hints.append(f"{os.path.basename(fp)}: 无限循环（需 break 条件）")
            if "File::open" in src or "fs::" in src and "read_to_string" in src:
                rule_hints.append(f"{os.path.basename(fp)}: 文件 IO（每帧循环内卡顿）")
        except OSError:
            continue
    # 预测结论
    affected = sorted(files.items(), key=lambda kv: -kv[1])
    risk = "high" if len(files) > 5 or lessons else (
        "medium" if len(files) > 1 else "low")
    return {"ok": True, "symbol": symbol, "file_hint": file_hint,
            "predict": {"affected_files": affected[:20],
                        "affected_count": len(files),
                        "definition_count": r.get("definition_count", 0),
                        "reference_count": r.get("reference_count", 0)},
            "lessons": lessons[:3],
            "rule_hints": rule_hints[:5],
            "risk": risk,
            "advice": (f"改前预测：{len(files)} 个文件受影响（{'high' if risk == 'high' else 'medium' if risk == 'medium' else 'low'} 风险）"
                       f"{'——命中教训库，改前先看教训' if lessons else ''}"
                       "——改后跑 ide_fusion impact 双引擎校验确认")}


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
