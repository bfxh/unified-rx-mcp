#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quest_auto —— ide_quest auto 链六步编排（2026-08-15 从 server 拆出）。

diagnose(bug_scan) → locate → impact → fix → verify → lesson。
依赖 server 运行时函数（_call/_TC/_check_path）——函数内延迟 import 防循环。
"""
import os
import re
import json


def run_auto(args: dict, quest_id: str) -> dict:
    """执行自动诊断链（六步）——返回 {ok, quest_id, chain, summary, ...}。"""
    from server import _call, _TC, _check_path  # noqa: 延迟 import 防循环
    from ide_quest import Quest, new_quest, resume_quest
    action = "auto"  # run_auto 专用于 auto 链
    # IDE 增强七（2026-08-13）：端到端自动推进链——
    # diagnose(bug_scan) → locate(问题 file:line) → impact(文件影响面)
    # → fix(ide_actions) → verify(回归提示)。一次调用跑完五步，
    # 结果写入 quest 状态（可断点续查）。
    import time as _t
    _chain_t0 = _t.perf_counter()
    path = str(args.get("path", ""))
    if not path:
        return {"ok": False, "error": "auto 需要 path 参数"}
    if not quest_id:
        quest_id = f"auto-{int(_t.time_ns())}"  # 纳秒防同秒碰撞（探针：秒级同秒复用旧任务）
    # IDE 增强十六：force=True 重置 quest 后重跑整链（上次诊断失败/不完整时重试）
    if args.get("force") and os.path.exists(Quest._state_path(quest_id)):
        q = new_quest(quest_id, str(args.get("task", "")) or f"自动诊断 {path}", path)
        q._save()
    q = resume_quest(quest_id)
    if q is None:
        q = new_quest(quest_id, str(args.get("task", "")) or f"自动诊断 {path}", path)
        q._save()
    chain: list[dict] = []

    # IDE 增强二十二：各步耗时（chain 每项附 elapsed_s——性能分布可视化）
    _last_step_ts = _t.perf_counter()

    def _finish(step_result: dict, step_name: str, tool: str, summary: str) -> None:
        nonlocal _last_step_ts
        now = _t.perf_counter()
        step_elapsed = round(now - _last_step_ts, 3)
        _last_step_ts = now
        r = q.complete_step(step_result)
        chain.append({"step": step_name, "tool": tool,
                      "summary": summary, "ok": r.get("ok", True),
                      "elapsed_s": step_elapsed})

    # 1. diagnose：bug_scan（IDE 增强十四：幂等只读重试一次——
    #    扩展懒加载/首扫慢等瞬时失败不拖垮整链）
    def _run_scan() -> dict:
        try:
            return json.loads(_call("bug_scan", {"path": path})[0].text)
        except (json.JSONDecodeError, IndexError, KeyError):
            return {"ok": False, "issues": []}

    scan_data = _run_scan()
    if not scan_data.get("ok"):
        scan_data = _run_scan()  # 重试一次（bug_scan 幂等只读）
    issues = scan_data.get("issues", []) if scan_data.get("ok") else []
    errors = [i for i in issues
              if str(i.get("severity", "")).lower() in ("error", "critical")]
    _finish({"tool": "bug_scan", "path": path, "issue_count": len(issues),
             "error_count": len(errors),
             "severity_counts": scan_data.get("severity_counts", {})},
            "diagnose", "bug_scan",
            f"{len(issues)} 问题（{len(errors)} error）"
            + ("" if scan_data.get("ok")
               else " ⚠ 扫描失败（可用 force=True 重试整链）"))  # IDE 增强二十四
    # IDE 增强五十七：std_check 工程标准联动（占位/死代码/重复定义/
    # UI 硬编码/魔法数字——独立于 bug_scan 主线，附 summary 不抢 locate）
    # 注意：不调 complete_step——双 diagnose 步会按名覆盖 bug_scan
    # result（verify_fix 基线丢失回归）；独立存 q.state["std_check"]。
    _std_sev = {}
    try:
        _std = json.loads(_call("std_check", {"path": path})[0].text)
        _std_sev = _std.get("severity_counts", {}) if _std.get("ok") is not None else {}
        if not isinstance(_std_sev, dict):
            _std_sev = {}
    except Exception:
        _std_sev = {}
    if _std_sev:
        _now = _t.perf_counter()
        chain.append({"step": "diagnose", "tool": "std_check",
                      "summary": (f"工程标准 {_std_sev.get('Critical', 0)} Critical/"
                                  f"{_std_sev.get('Error', 0)} Error/"
                                  f"{_std_sev.get('Warning', 0)} Warning"),
                      "ok": True, "elapsed_s": round(_now - _last_step_ts, 3)})
        _last_step_ts = _now
        q.state["std_check"] = {"severity_counts": _std_sev, "ts": _t.time()}
        q._save()
    # 2. locate：top 问题位置（error 优先）+ 行上下文 + 符号线索（IDE 增强十）
    top = (errors or issues or [{}])[0]
    loc = {"tool": "locate", "file": top.get("file", ""),
           "line": top.get("line", 0), "rule": top.get("rule", ""),
           "message": str(top.get("msg", ""))[:120],
           "severity": str(top.get("severity", "")).lower() or "unknown"}  # IDE 增强三十七
    if loc["file"] and os.path.isfile(loc["file"]):
        try:
            with open(loc["file"], encoding="utf-8", errors="replace") as f:
                f_lines = f.readlines()
            ln = int(loc.get("line", 0) or 0)
            ctx: list[str] = []
            for i in range(max(1, ln - 2), min(len(f_lines), ln + 2) + 1):
                ctx.append(f"{i}: {f_lines[i - 1].rstrip()}")
            loc["context"] = ctx
            if 1 <= ln <= len(f_lines):
                # IDE 增强九十六（探针抓出两轮）：symbol_hint 先取
                # 「第一个函数调用目标」（`foo().unwrap()` → foo），
                # 无调用再回退第一个非关键字标识符——影响面线索准确
                _KW = {"let", "const", "var", "fn", "pub", "mut", "static",
                       "use", "mod", "struct", "enum", "trait", "impl",
                       "def", "class", "function", "if", "else", "for",
                       "while", "return", "import", "from", "new", "void",
                       "int", "float", "double", "char", "bool", "true",
                       "false", "none", "self", "this", "super", "as",
                       "in", "is", "of", "match", "ref", "unsafe"}
                _line_txt = f_lines[ln - 1]
                _call_m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                                    _line_txt)
                _hint = ""
                if _call_m and _call_m.group(1).lower() not in _KW:
                    _hint = _call_m.group(1)
                else:
                    for _m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\b",
                                          _line_txt):
                        if _m.group(1).lower() not in _KW:
                            _hint = _m.group(1)
                            break
                loc["symbol_hint"] = _hint
                # IDE 增强九十五：符号引用数（影响面线索——AI 一眼看到
                # 这个符号被多少处引用，判断修复影响范围）
                if loc.get("symbol_hint"):
                    try:
                        from ide_tools import _find_symbol_refs
                        _refs = _find_symbol_refs(
                            str(args.get("path", "")), loc["symbol_hint"],
                            300, exclude_comments=True)
                        loc["symbol_refs"] = len(_refs)
                    except Exception:
                        loc["symbol_refs"] = 0
        except OSError:  # 尽力而为
            pass
    _finish(loc, "locate", "locate",
            f"{loc['file']}:{loc['line']} [{loc['rule']}]" if loc["file"] else "未发现问题")
    # 3. impact：文件级影响面 + 尽力接 cae_change_impact（符号级深化）
    # IDE 增强三十二：mode=quick 跳过 change_impact（快路径）；full（默认）深查
    mode = str(args.get("mode", "full"))
    file_issues = [i for i in issues if i.get("file") == loc["file"]]
    impact_result = {"tool": "impact", "file": loc["file"],
                     "file_issue_count": len(file_issues),
                     "note": "文件级影响面；符号级深化用 change_impact/lsp_query"}
    # IDE 增强六十五：impact 附 std_check 文件级分布（工程标准在该文件
    # 的严重度——修复目标文件的标准问题一览）
    if loc.get("file") and os.path.isfile(loc["file"]):
        try:
            _std_f = json.loads(
                _call("std_check", {"path": loc["file"]})[0].text)
            impact_result["std_file_severity"] = (
                _std_f.get("severity_counts") or {})
        except Exception:
            pass  # std 单文件扫描失败静默
    if mode == "full" and os.path.isdir(path) and loc.get("file"):
        try:
            rel = os.path.relpath(loc["file"], path)
            ci = json.loads(_call("cae_change_impact",
                                  {"repo_path": path,
                                   "changed_files": [rel]})[0].text)
            r0 = (ci.get("results") or [{}])[0]
            if ci.get("ok") and r0.get("ok"):
                impact_result["change_impact"] = {
                    "symbols": r0.get("symbols", [])[:10],
                    "referenced_by_count": len(r0.get("referenced_by", [])),
                    "suggested_tests": r0.get("suggested_tests", []),
                }
                impact_result["note"] = ("符号级影响面（cae_change_impact）"
                                         "——调用方/建议测试见 change_impact")
        except Exception:
            pass  # 扩展不可用/超时 → 降级文件级影响面
    _finish(impact_result, "impact", "impact",
            f"{loc['file']} 共 {len(file_issues)} 个问题" +
            (f"，影响 {impact_result['change_impact']['referenced_by_count']} 处引用"
             if "change_impact" in impact_result else ""))
    # 4. fix：ide_actions 单文件
    if loc["file"] and os.path.isfile(loc["file"]):
        try:
            fix_data = json.loads(_call("ide_actions", {"path": loc["file"]})[0].text)
        except (json.JSONDecodeError, IndexError, KeyError):
            fix_data = {"ok": False, "actions": []}
        actions = fix_data.get("actions", []) if fix_data.get("ok") else []
        _finish({"tool": "ide_actions", "file": loc["file"],
                 "action_count": len(actions),
                 "actions": [{"line": a.get("line"), "title": a.get("title"),
                              "detail": str(a.get("detail", ""))[:150]}
                             for a in actions[:10]],
                 # IDE 增强八：可直接粘贴的 fs_write 骨架（L4 授权一步应用）
                 "fs_template": {
                     "tool": "fs_write",
                     "args": {"path": loc["file"],
                              "content": "<读取原文件，按 actions 行号应用建议后写回>"},
                     "auth_hint": "L4 授权：参数加 __authorized: true",
                     # IDE 增强七十七：行号预览（模板应用范围一览）
                     "lines": [{"line": a.get("line"),
                                "title": str(a.get("title", ""))[:24]}
                               for a in actions[:10]],
                 }},
                "fix", "ide_actions",
                f"{len(actions)} 条修复建议" +
                (f"（{' / '.join(str(a.get('title', ''))[:20] for a in actions[:2])}）"  # IDE 增强四十八：摘要附建议标题
                 if actions else ""))
    else:
        _finish({"tool": "ide_actions", "file": loc["file"], "action_count": 0,
                 "skipped": True}, "fix", "ide_actions", "无目标文件，跳过")
    # 5. verify：回归提示 + 修复后自检清单（IDE 增强九）
    ext = os.path.splitext(loc.get("file", ""))[1].lower()
    cmd = ("cargo test" if ext == ".rs"
           else "pytest" if ext in (".py",) else "构建/测试")
    # IDE 增强五十二：修复前 diff 摘要（影响行号区间，修复后对照）
    _fix_locs = sorted({(a.get("line"), a.get("line_end", a.get("line")))
                        for a in (actions if "actions" in dir() else [])[:10]})
    _fix_scope = "、".join(f"L{s}" if s == e else f"L{s}-L{e}"
                           for s, e in _fix_locs[:6]) or "无"
    _finish({"tool": "verify",
             "advice": f"应用 fix 步的修复建议后跑 `{cmd}` 回归；"
                       f"完成后可用 ide_quest note 记录结果",
             "command": cmd,
             "fix_scope": f"{len(_fix_locs)} 处（{_fix_scope}）",
             # IDE 增强九：修复后自检清单（逐项确认防遗漏）
             "checklist": [
                 f"1. 应用 fix 步的修复建议/fs_template 到 {loc.get('file', '目标文件')}",
                 f"2. 跑回归：{cmd}",
                 "3. 复查：修复行不再触发原规则（用 `ide_quest action=verify_fix` 验证）",
                 "4. 通过后用 ide_quest note 记录验证结果",
                 "5. lesson 步提示：用 lesson_recall 记录教训防复发",
                 # IDE 增强 122：UI 项目回归提示（修复涉及 UI 文件时）
                 "6. 若修复涉及 UI 代码（hud/ui/panel）：跑 `ui_check` 确认无新 UI 问题",
             ]},
            "verify", "verify", f"回归命令：{cmd}，自检清单 5 项")  # IDE 增强四十六：summary 附清单计数
    # 6. lesson：自动链收尾（STEPS 六步闭环）
    # IDE 增强四十七：lesson summary 附 fix 计数（链摘要看到修复工作量）
    _lesson_fix_n = (len(actions) if "actions" in dir() else 0)
    _finish({"tool": "lesson",
             # IDE 增强八十：附 auto_extract 提示（文本教训自动提取入库）
             "advice": "修复验证通过后建议用 lesson_recall 记录教训防复发；"
                       "对话文本教训可用 auto_extract_lessons 自动提取入库",
             # IDE 增强五十一：lesson 附 fix 计数（report 显示修复工作量）
             "fix_count": _lesson_fix_n,
             # IDE 增强七十五：lesson_recall 联动字段（消费端直接可调）
             "recall": {"tool": "lesson_recall_lse",
                        "args": {"task_description":
                                 str(args.get("task", ""))},
                        "hint": "修复验证后召回同类教训防复发"}},
            "lesson", "lesson",
            f"教训提示（{_lesson_fix_n} 条修复建议后 lesson_recall 记录）")
    # 顶层 summary（IDE 增强十一 2026-08-13）：六步一句话总览——
    # AI 一眼看到全链结论；完整结果仍在 quest 状态可断点续查
    chain_summary = " → ".join(c.get("summary", "") for c in chain)
    # IDE 增强五十五：summary 长度上限（防 token 膨胀——截断保留头尾
    # 关键信息：前部=diagnose/locate 定位、尾部=verify/lesson 结论）
    if len(chain_summary) > 300:
        chain_summary = chain_summary[:150] + "…" + chain_summary[-140:]
    if mode == "quick":
        # IDE 增强四十九：quick 模式链摘要前缀标注（消费端一眼区分快路径）
        chain_summary = f"⚡quick {chain_summary}"
    chain_elapsed = round(_t.perf_counter() - _chain_t0, 2)  # IDE 增强十五：链耗时
    # IDE 增强二十五：结果总判定（success/partial/failed）——AI 一眼知道链成败
    _diag_ok = bool(scan_data.get("ok"))
    _has_issue = len(issues) > 0
    _skipped = any("跳过" in c.get("summary", "") for c in chain)
    if not _diag_ok:
        result_verdict = "failed"
        result_note = "诊断扫描失败（重试后仍失败），可用 force=True 重跑整链"
    elif _has_issue:
        result_verdict = "partial"
        # IDE 增强三十八：result_note 附 severity 统计（分布一眼可见）
        _sev = scan_data.get("severity_counts", {}) or {}
        result_note = (f"发现问题 {len(issues)} 个（error {len(errors)}"
                       + f"；severity: error {_sev.get('error', 0)}/"
                       + f"warn {_sev.get('warn', 0)}/info {_sev.get('info', 0)}）"
                       + ("，部分步骤跳过" if _skipped else "")
                       + "——修复建议见 fix 步，应用后 verify_fix 验证")
    else:
        result_verdict = "success"
        result_note = "未发现问题——链路正常"
    # IDE 增强二十七：顶层 summary 附 result 前缀（扫 log/汇报一眼见成败）
    chain_summary = f"[{result_verdict}] {chain_summary}"
    # IDE 增强六十四：result_note 附 quest_id（断点续跑/verify_fix 直达）
    result_note = f"{result_note}（quest_id: {quest_id}）"
    # IDE 增强十二：auto 完成 → scan-log 落盘（链路记忆，项目维度可查）
    try:
        import scan_log_core as _slc
        _slc.append_scan({"tool": "ide_quest_auto", "root": path,
                          "ok": True, "summary": chain_summary[:200]})
    except Exception:
        pass  # 日志失败静默（不拖垮 auto 链）
    # IDE 增强十九：markdown 报告（human-readable，AI 可直接展示/粘贴）
    # IDE 增强十九：markdown 报告（2026-08-15 构造逻辑拆出——
    # ide_quest.build_auto_report——_tool_ide_quest CC=164 瘦身）
    from ide_quest import build_auto_report
    report_md = build_auto_report({
        "args": args, "path": path, "quest_id": quest_id,
        "mode": mode, "chain": chain, "result_note": result_note,
        "scan_data": scan_data, "loc": loc, "std_sev": _std_sev,
        "result_verdict": result_verdict, "chain_elapsed": chain_elapsed,
        "log_path": _slc.log_path() if '_slc' in dir() else '~/.unified-rx/scan-log.jsonl',
        "notes_count": len(q.state.get("notes", [])),
    })
    # IDE 增强二十：报告摘要入 quest note（断点续跑可见上轮报告）
    try:
        q.add_note(f"自动诊断报告（{chain_elapsed}s）：{chain_summary[:300]}")
    except Exception:
        pass  # 备注失败静默            # IDE 增强二十八：报告落盘文件（项目 .unified-rx-index/reports/——
    # 独立于 quest 状态，项目维度可直接查看/归档）
    report_path = None
    try:
        _base = path if os.path.isdir(path) else os.path.dirname(path)
        _rep_dir = os.path.join(_base, ".unified-rx-index", "reports")
        os.makedirs(_rep_dir, exist_ok=True)
        _rep_file = os.path.join(_rep_dir, f"auto-{int(_t.time())}.md")
        with open(_rep_file, "w", encoding="utf-8") as _f:
            _f.write(report_md)
        report_path = _rep_file
        # IDE 增强二十九：只保留最近 N 份报告（防目录膨胀）
        try:
            _MAX_REPORTS = 20
            _reports = sorted(os.listdir(_rep_dir))
            for _old in _reports[:-_MAX_REPORTS] if len(_reports) > _MAX_REPORTS else []:
                os.remove(os.path.join(_rep_dir, _old))
        except Exception:
            pass  # 清理失败静默
    except Exception:
        pass  # 落盘失败静默（报告仍经返回值/note/scan-log 可查）
    # IDE 增强四十一：note 附报告落盘路径（项目维度可直达文件）
    if report_path:
        try:
            q.add_note(f"报告文件：{report_path}")
        except Exception:
            pass  # 备注失败静默
    return {"ok": True, "quest_id": quest_id,
                            "chain": chain,
                            "summary": chain_summary,
                            "elapsed_s": chain_elapsed,
                            "result": result_verdict,
                            "result_note": result_note,
                            # IDE 增强六十：双引擎分布顶层字段（消费端
                            # 一眼看 bug_scan + std_check 全貌）
                            "std_severity_counts": _std_sev,
                            "report_md": report_md,
                            "report_path": report_path,
                            # IDE 增强九十一：任务备注数（消费端看协作痕迹）
                            "notes_count": len(q.state.get("notes", [])),
                            "status": q.status()}
