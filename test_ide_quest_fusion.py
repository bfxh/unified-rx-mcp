"""test_ide_quest_fusion.py — IDE R6/R7 测试（IDE_ENHANCE_PLAN R6+R7）。

覆盖：
  R6：annotate_issues 符号聚合 / cross_validate_impact 双引擎校验
  R7：Quest 状态机——new→step×6→finished + 断点续跑 + list
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ide_fusion  # noqa: E402
import ide_quest  # noqa: E402
import server  # noqa: E402


# ── R6 ──
def test_annotate_issues(tmp_path):
    f = tmp_path / "a.rs"
    f.write_text("fn alpha() {\n    let x = 1 as u8;\n}\nfn beta() {\n    let y = 2 as u8;\n}\n")
    issues = [
        {"file": str(f), "line": 2, "kind": "as", "message": "as u8"},
        {"file": str(f), "line": 5, "kind": "as", "message": "as u8"},
    ]
    r = ide_fusion.annotate_issues(str(tmp_path), issues)
    assert r["total"] == 2
    assert r["symbol_map"][f"{f}#alpha"] == 1
    assert r["symbol_map"][f"{f}#beta"] == 1


def test_cross_validate_impact():
    r = ide_fusion.cross_validate_impact(
        "/repo", "f", ["a.rs", "b.rs"], ["b.rs", "c.rs"])
    assert r["lsp_only"] == ["a.rs"]
    assert r["tree_only"] == ["c.rs"]
    assert r["both"] == ["b.rs"]
    assert "差异" in r["verdict"]


# ── R7 ──
def test_quest_full_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q1", "修 UI bug", "/repo")
    assert q.current_step_name() == "diagnose"
    names = ["diagnose", "locate", "impact", "fix", "verify", "lesson"]
    for i, name in enumerate(names):
        r = q.complete_step({"evidence": f"step {name} done"})
        assert r["completed"] == name
    assert q.status()["finished"] is True
    assert q.current_step_name() == "done"


def test_quest_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q2", "任务", "/repo")
    q.complete_step({"e": 1})
    q.complete_step({"e": 2})
    # 断点续跑
    q2 = ide_quest.resume_quest("q2")
    assert q2 is not None
    assert q2.current_step_name() == "impact"
    assert q2.status()["done_steps"] == ["diagnose", "locate"]


def test_quest_list(tmp_path, monkeypatch):
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    ide_quest.new_quest("q3", "任务3", "/r")._save()
    lst = ide_quest.list_quests()
    assert len(lst) == 1
    assert lst[0]["quest_id"] == "q3"


def test_quest_abort_and_note(tmp_path, monkeypatch):
    """IDE 增强（2026-08-13）：abort 放弃 + note 备注 + status 时长/备注计数。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    q = ide_quest.new_quest("q4", "任务4", "/r")
    # 备注
    r = q.add_note("发现：根因在 removal.rs 碎片归集")
    assert r["ok"] and r["notes_count"] == 1
    assert q.status()["notes_count"] == 1
    # 空备注拒绝
    assert q.add_note("   ")["ok"] is False
    # 状态含 elapsed_s
    st = q.status()
    assert "elapsed_s" in st and st["elapsed_s"] >= 0
    assert st["aborted"] is False
    # abort：标记放弃、保留状态可复盘
    r = q.abort()
    assert r["ok"] and r["aborted"] is True
    assert q.status()["aborted"] is True
    # 二次 abort 拒绝
    assert q.abort()["ok"] is False
    # 断点续跑后仍可见 aborted + notes
    q2 = ide_quest.resume_quest("q4")
    assert q2 is not None and q2.status()["aborted"] is True
    assert q2.status()["notes_count"] == 1


def test_quest_tool_abort_note_integration(tmp_path, monkeypatch):
    """ide_quest 工具 abort/note action 端到端。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    r = server._call("ide_quest", {"action": "new", "quest_id": "qi4",
                                    "task": "任务", "repo": "/x"})
    assert json.loads(r[0].text)["ok"]
    r = server._call("ide_quest", {"action": "note", "quest_id": "qi4",
                                    "text": "上下文备注"})
    d = json.loads(r[0].text)
    assert d["ok"] and d["notes_count"] == 1
    r = server._call("ide_quest", {"action": "abort", "quest_id": "qi4"})
    assert json.loads(r[0].text)["ok"] is True
    r = server._call("ide_quest", {"action": "status", "quest_id": "qi4"})
    assert json.loads(r[0].text)["status"]["aborted"] is True


def test_quest_auto_chain(tmp_path, monkeypatch):
    """IDE 增强七：ide_quest action=auto 六步自动链端到端。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    # 目标建在 cwd（server sandbox 默认根）内——绕过沙盒（server 模块已 import，
    # setenv 不生效）；用后清理
    repo = tempfile.mkdtemp(prefix="quest_auto_", dir=os.getcwd())
    try:
        with open(os.path.join(repo, "bug.rs"), "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().unwrap();\n    let y = bar().unwrap();\n}\n")
        r = server._call("ide_quest", {"action": "auto", "path": repo,
                                       "task": "修 bug.rs"})
        d = json.loads(r[0].text)
        assert d["ok"], d
        assert d["status"]["finished"] is True, "六步跑完应 finished"
        # IDE 增强十一：顶层 summary 一句话总览（省 token）
        assert "summary" in d and "bug.rs" in d["summary"], f"summary 应含定位: {d.get('summary')}"
        assert "cargo test" in d["summary"], f"summary 应含回归命令: {d.get('summary')}"
        # IDE 增强十五：链耗时统计
        assert "elapsed_s" in d and d["elapsed_s"] >= 0, f"应附链耗时: {d.get('elapsed_s')}"
        # IDE 增强十九：markdown 报告
        assert "report_md" in d and "自动诊断报告" in d["report_md"], "应附 markdown 报告"
        assert "### 定位" in d["report_md"] and "### 修复建议" in d["report_md"], (
            f"报告应含步骤小节: {d['report_md'][:200]}")
        # IDE 增强二十三：报告附耗时分布表
        assert "耗时分布" in d["report_md"] and "| 步骤 | 耗时 |" in d["report_md"], (
            f"报告应含耗时表: {d['report_md'][:200]}")
        # IDE 增强二十：报告摘要入 quest note（断点续跑可见）
        qn = ide_quest.resume_quest(d["quest_id"])
        assert qn.status()["notes_count"] >= 1, "报告摘要应入 note"
        assert any("自动诊断报告" in n["text"] for n in qn.state.get("notes", [])), (
            f"note 应含报告摘要: {qn.state.get('notes')}")
        # IDE 增强二十一：action=report 导出完整 markdown 报告
        rp = json.loads(server._call("ide_quest", {"action": "report",
                                                   "quest_id": d["quest_id"]})[0].text)
        assert rp["ok"] and "report_md" in rp, "report 应返回 markdown"
        assert "## 修复建议" in rp["report_md"] and "## 验证" in rp["report_md"], (
            f"报告应含步骤: {rp['report_md'][:200]}")
        assert rp["finished"] is True
        # IDE 增强十二：auto 完成落盘 scan-log（链路记忆，项目维度可查）
        monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(tmp_path / "scan-log.jsonl"))
        server._call("ide_quest", {"action": "auto", "path": repo, "task": "修 bug.rs 2"})
        log_text = (tmp_path / "scan-log.jsonl").read_text(encoding="utf-8", errors="replace")
        assert "ide_quest_auto" in log_text, f"auto 完成应落盘 scan-log，实际: {log_text[:300]!r}"
        # summary 落盘截断 200 字符——断言链前部内容（定位），不断言尾部回归命令
        assert "bug.rs" in log_text, f"scan-log 应含链摘要（定位）: {log_text[:300]!r}"
        steps = [c["step"] for c in d["chain"]]
        assert steps == ["diagnose", "locate", "impact", "fix", "verify", "lesson"], f"链顺序: {steps}"
        # IDE 增强二十二：每步附耗时
        for c in d["chain"]:
            assert "elapsed_s" in c and c["elapsed_s"] >= 0, f"每步应附耗时: {c}"
        # diagnose 记录问题数（bug_scan 对 unwrap 至少报 warn）
        diag = next(c for c in d["chain"] if c["step"] == "diagnose")
        assert diag["ok"] is True
        # locate 定位到 bug.rs 的具体行 + 上下文 + 符号线索（IDE 增强十）
        loc = next(c for c in d["chain"] if c["step"] == "locate")
        assert "bug.rs" in loc["summary"], f"locate 应定位 bug.rs: {loc['summary']}"
        ql = ide_quest.resume_quest(d["quest_id"])
        loc_result = ql.state["steps"]["locate"]["result"]
        assert "context" in loc_result and len(loc_result["context"]) >= 1, (
            f"locate 应附行上下文: {loc_result}")
        assert loc_result.get("symbol_hint", ""), "locate 应提取符号线索"
        # fix 生成修复建议（unwrap → 安全处理）
        fix = next(c for c in d["chain"] if c["step"] == "fix")
        assert fix["summary"].startswith("2 条修复建议"), f"两个 unwrap 应有 2 条建议: {fix}"
        # IDE 增强八：fix 步 result 附 fs_template（fs_write 骨架 + L4 授权提示）
        q = ide_quest.resume_quest(d["quest_id"])
        fix_result = q.state["steps"]["fix"]["result"]
        assert "fs_template" in fix_result, f"fix 步应附 fs_template: {fix_result}"
        tpl = fix_result["fs_template"]
        assert tpl["tool"] == "fs_write"
        assert tpl["args"]["path"].endswith("bug.rs"), f"模板路径: {tpl}"
        assert "__authorized" in tpl.get("auth_hint", ""), "应含 L4 授权提示"
        # verify 给回归命令 + 自检清单
        ver = next(c for c in d["chain"] if c["step"] == "verify")
        assert "cargo test" in ver["summary"], f"Rust 文件应建议 cargo test: {ver}"
        qv = ide_quest.resume_quest(d["quest_id"])
        ver_result = qv.state["steps"]["verify"]["result"]
        assert "checklist" in ver_result and len(ver_result["checklist"]) >= 3, (
            f"verify 步应附自检清单: {ver_result}")
        # 断点续查：quest 状态保留六步结果
        r2 = server._call("ide_quest", {"action": "status", "quest_id": d["quest_id"]})
        st = json.loads(r2[0].text)["status"]
        assert st["finished"] is True and len(st["done_steps"]) == 6
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_quest_result_retrieval(tmp_path, monkeypatch):
    """IDE 增强十三：action=result 精确检索某步完整结果（省 token）。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    repo = tempfile.mkdtemp(prefix="quest_result_", dir=os.getcwd())
    try:
        with open(os.path.join(repo, "bug.rs"), "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().unwrap();\n}\n")
        r = server._call("ide_quest", {"action": "auto", "path": repo})
        qid = json.loads(r[0].text)["quest_id"]
        # 检索 fix 步完整结果（fs_template 等大字段）
        r2 = server._call("ide_quest", {"action": "result", "quest_id": qid,
                                        "step": "fix"})
        d = json.loads(r2[0].text)
        assert d["ok"] and d["step"] == "fix" and d["done"] is True
        assert "fs_template" in d["result"], f"fix 步结果应含 fs_template: {d}"
        # 未知步骤 → 明确错误 + available 列表
        r3 = server._call("ide_quest", {"action": "result", "quest_id": qid,
                                        "step": "nope"})
        d3 = json.loads(r3[0].text)
        assert not d3["ok"] and "available" in d3
        # 不存在的 quest → 错误
        r4 = server._call("ide_quest", {"action": "result", "quest_id": "no-such",
                                        "step": "fix"})
        assert not json.loads(r4[0].text)["ok"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_quest_auto_force_rerun(tmp_path, monkeypatch):
    """IDE 增强十六：auto force=True 重置 quest 重跑整链（失败诊断重试）。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    repo = tempfile.mkdtemp(prefix="quest_force_", dir=os.getcwd())
    try:
        with open(os.path.join(repo, "bug.rs"), "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().unwrap();\n}\n")
        r1 = server._call("ide_quest", {"action": "auto", "path": repo,
                                        "quest_id": "force-q1"})
        d1 = json.loads(r1[0].text)
        assert d1["ok"] and d1["status"]["finished"] is True
        # 修改文件后 force 重跑：quest 重置 + 新链结果
        with open(os.path.join(repo, "bug.rs"), "a", encoding="utf-8") as f:
            f.write("fn main2() { let y = bar().unwrap(); }\n")
        r2 = server._call("ide_quest", {"action": "auto", "path": repo,
                                        "quest_id": "force-q1", "force": True})
        d2 = json.loads(r2[0].text)
        assert d2["ok"] and d2["status"]["finished"] is True
        assert d2["quest_id"] == "force-q1"
        # 重置后 fix 步 result 是新链的（fs_template 仍在）
        q = ide_quest.resume_quest("force-q1")
        fix_result = q.state["steps"]["fix"]["result"]
        assert "fs_template" in fix_result
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_quest_list_filter_and_clean(tmp_path, monkeypatch):
    """IDE 增强十七：list 过滤（active/finished/aborted）+ clean 清理。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    repo = tempfile.mkdtemp(prefix="quest_clean_f_", dir=os.getcwd())
    try:
        with open(os.path.join(repo, "bug.rs"), "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().unwrap();\n}\n")
        # 1 个 finished（auto 跑完）+ 1 个 active（new 不跑）+ 1 个 aborted
        r = server._call("ide_quest", {"action": "auto", "path": repo, "quest_id": "cf-f"})
        assert json.loads(r[0].text)["ok"]
        server._call("ide_quest", {"action": "new", "quest_id": "cf-a", "task": "active", "repo": "/x"})
        server._call("ide_quest", {"action": "abort", "quest_id": "cf-a"})
        server._call("ide_quest", {"action": "new", "quest_id": "cf-b", "task": "active2", "repo": "/x"})
        # 过滤
        r2 = json.loads(server._call("ide_quest", {"action": "list", "status": "active"})[0].text)
        assert {q["quest_id"] for q in r2["quests"]} == {"cf-b"}, f"active 应只含 cf-b: {r2}"
        r3 = json.loads(server._call("ide_quest", {"action": "list", "status": "aborted"})[0].text)
        assert {q["quest_id"] for q in r3["quests"]} == {"cf-a"}
        # clean：清理 finished + aborted，保留 active
        r4 = json.loads(server._call("ide_quest", {"action": "clean"})[0].text)
        assert r4["ok"] and r4["removed"] == 2, f"应清理 2 个: {r4}"
        r5 = json.loads(server._call("ide_quest", {"action": "list"})[0].text)
        assert {q["quest_id"] for q in r5["quests"]} == {"cf-b"}, f"clean 后只留 active: {r5}"
        # 未知过滤 → 错误
        r6 = json.loads(server._call("ide_quest", {"action": "list", "status": "nope"})[0].text)
        assert not r6["ok"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_quest_verify_fix(tmp_path, monkeypatch):
    """IDE 增强十八：verify_fix 应用验证器（重扫目标文件对比问题数）。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    repo = tempfile.mkdtemp(prefix="quest_vfix_", dir=os.getcwd())
    bug_file = os.path.join(repo, "bug.rs")
    try:
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().unwrap();\n    let y = bar().unwrap();\n}\n")
        r = server._call("ide_quest", {"action": "auto", "path": repo, "quest_id": "vf-1"})
        qid = json.loads(r[0].text)["quest_id"]
        # 未修复：verify_fix 报告当前问题数 + verdict 未变化
        r2 = json.loads(server._call("ide_quest", {"action": "verify_fix",
                                                   "quest_id": qid})[0].text)
        assert r2["ok"] and r2["file"].endswith("bug.rs")
        assert r2["issue_count"] >= 1, f"未修复应有问题: {r2}"
        assert r2["prev_issue_count"] >= 1
        # 模拟修复（去掉 unwrap）后：问题数下降 → verdict 修复生效
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("fn main() {\n    let x = foo().ok();\n    let y = bar().ok();\n}\n")
        r3 = json.loads(server._call("ide_quest", {"action": "verify_fix",
                                                   "quest_id": qid})[0].text)
        assert r3["ok"] and r3["issue_count"] < r3["prev_issue_count"], f"修复后应减少: {r3}"
        assert r3["verdict"] == "修复生效", f"verdict: {r3['verdict']}"
        # 不存在 quest → 错误
        r4 = json.loads(server._call("ide_quest", {"action": "verify_fix",
                                                   "quest_id": "no-such"})[0].text)
        assert not r4["ok"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_quest_auto_no_issues(tmp_path, monkeypatch):
    """auto 链对干净代码：六步仍走完，locate 标记未发现问题。"""
    import shutil
    import tempfile
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    repo = tempfile.mkdtemp(prefix="quest_clean_", dir=os.getcwd())
    try:
        with open(os.path.join(repo, "clean.rs"), "w", encoding="utf-8") as f:
            f.write("fn main() { let x = 1; }\n")
        r = server._call("ide_quest", {"action": "auto", "path": repo})
        d = json.loads(r[0].text)
        assert d["ok"]
        loc = next(c for c in d["chain"] if c["step"] == "locate")
        assert "未发现" in loc["summary"], f"干净代码应标记未发现问题: {loc['summary']}"
        assert d["status"]["finished"] is True
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ide_quest_tool_integration(tmp_path, monkeypatch):
    """ide_quest 工具端到端（new → step×6 → finished）。"""
    monkeypatch.setattr(ide_quest, "_QUEST_DIR", str(tmp_path))
    r = server._call("ide_quest", {"action": "new", "quest_id": "qi1",
                                    "task": "修地形 bug", "repo": "/x"})
    d = json.loads(r[0].text)
    assert d["ok"] and d["status"]["current"] == "diagnose"
    for _ in range(6):
        r = server._call("ide_quest", {"action": "step", "quest_id": "qi1",
                                       "result": {"done": True}})
    d = json.loads(r[0].text)
    assert d.get("finished") is True
    r = server._call("ide_quest", {"action": "list"})
    d = json.loads(r[0].text)
    assert any(q["quest_id"] == "qi1" and q["finished"] for q in d["quests"])


def test_ide_fusion_tool_integration():
    r = server._call("ide_fusion", {"path": r"D:\开发\VoxelForge-Nexus"})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert d["total"] > 0


def test_ide_fusion_impact_via_references(tmp_path):
    """IDE 增强四：双引擎影响面校验（tree 侧引用来自 ide_references）。"""
    repo = tmp_path
    (repo / "lib.rs").write_text(
        "pub fn compute_area(w: f32) -> f32 { w }\n"
        "fn main() {\n"
        "    let a = compute_area(2.0);\n"
        "    let b = compute_area(3.0);\n"
        "}\n",
        encoding="utf-8",
    )
    r = server._call("ide_fusion", {"path": str(repo), "action": "impact",
                                    "symbol": "compute_area"})
    d = json.loads(r[0].text)
    assert d["ok"], d
    assert d["definition_count"] == 1
    assert d["reference_count"] == 2, f"两个调用应计入: {d['reference_count']}"
    assert len(d["tree_refs"]) == 1, f"引用文件应只有 lib.rs: {d['tree_refs']}"
    assert "verdict" in d and "source" in d
    # 带 LSP 引用对比：一致 → verdict 一致
    r2 = server._call("ide_fusion", {"path": str(repo), "action": "impact",
                                     "symbol": "compute_area",
                                     "lsp_refs": [str(repo / "lib.rs")]})
    d2 = json.loads(r2[0].text)
    assert d2["verdict"] == "一致", f"LSP 与 tree 引用一致应判定一致: {d2['verdict']}"
