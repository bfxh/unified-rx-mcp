# -*- coding: utf-8 -*-
"""L3 A/B runner 离线回归：语料契约 / verdict 解析与校验 / 幻觉率 / judge 返回类型。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import ab_run  # noqa: E402
import registry  # noqa: E402
import tools  # noqa: F401,E402


# ---------- 语料契约 ----------

def test_corpus_loads_and_contract_holds():
    tasks = ab_run.load_tasks()
    assert len(tasks) == 12
    for t in tasks:
        assert t["rubric"], t["id"]
        for r in t["rubric"]:
            assert set(r) >= {"rid", "requirement", "gold"}
        # issue 描述不得泄露 gold——金标词不出现（松校验：最长 gold 片段）
        g = max((r["gold"] for r in t["rubric"]), key=len)
        head = g[:12]
        if len(g) >= 12 and head.isascii():   # 中文短语子串误报高，仅对英文指纹核对
            assert head.lower() not in t["issue"].lower(), t["id"]


def test_load_tasks_filter_and_missing_field():
    one = ab_run.load_tasks({"VF3-T01"})
    assert [t["id"] for t in one] == ["VF3-T01"]
    with pytest.raises(SystemExit):
        ab_run.load_tasks({"NOPE"})


# ---------- verdict 解析 / 校验 ----------

def test_parse_verdict_balanced_json_with_noise():
    txt = '评审如下：\n```json\n{"R1": "pass", "R2": "fail", "R3": "unverifiable"}\n```\n以上'
    assert ab_run.parse_verdict(txt)["R2"] == "fail"


def test_parse_verdict_rejects_garbage():
    with pytest.raises(ValueError):
        ab_run.parse_verdict("完全没有结构化内容")
    with pytest.raises(ValueError):
        ab_run.parse_verdict('{"R1": "pas')


def test_validate_verdict_drift_detection():
    ok = {"R1": "pass", "R2": "fail"}
    assert ab_run.validate_verdict(ok, ["R1", "R2"]) == ok
    with pytest.raises(ValueError):
        ab_run.validate_verdict({"rid": "pass"}, ["R1", "R2"])      # 键漂移（实发案例）
    with pytest.raises(ValueError):
        ab_run.validate_verdict({"1": "pass"}, ["R1"])              # 键漂移（实发案例）
    with pytest.raises(ValueError):
        ab_run.validate_verdict({"R1": "yes"}, ["R1"])              # 值域漂移


def test_judge_one_returns_dict_not_none(monkeypatch):
    """回归锁：judge_one 成功路径必须返回 dict（曾掉出函数尾静默返回 None）。"""
    def fake_chat(ch, model, messages, tools_schema=None, retries=1):
        return {"choices": [{"message": {"content":
                '{"R1":"pass","R2":"fail","R3":"unverifiable"}'}}]}
    monkeypatch.setattr(ab_run, "chat", fake_chat)
    task = {"id": "X", "issue": "i",
            "rubric": [{"rid": f"R{i}", "requirement": "r", "gold": "g"} for i in (1, 2, 3)]}
    out = ab_run.judge_one({}, "m", task, "answer")
    assert isinstance(out, dict) and out["R1"] == "pass"


def test_judge_one_retry_then_fail(monkeypatch):
    calls = []

    def fake_chat(ch, model, messages, tools_schema=None, retries=1):
        calls.append(messages[-1]["content"])
        return {"choices": [{"message": {"content": '{"rid":"pass"}'}}]}
    monkeypatch.setattr(ab_run, "chat", fake_chat)
    task = {"id": "X", "issue": "i",
            "rubric": [{"rid": "R1", "requirement": "r", "gold": "g"}]}
    with pytest.raises(ValueError):
        ab_run.judge_one({}, "m", task, "a")
    assert len(calls) == 2 and "不合规格" in calls[1]     # 第二次带纠偏提示


# ---------- 路径幻觉率 ----------

def test_halluc_rate_on_fake_root(tmp_path):
    (tmp_path / "crates").mkdir()
    real = tmp_path / "crates" / "terrain.rs"
    real.write_text("fn a() {}\n", encoding="utf-8")
    answers = ["见 `crates/terrain.rs:sym` 与 `src/fake/path.rs`"]
    claims, rate = ab_run.halluc_rate(answers, root=str(tmp_path))
    assert claims == 2 and abs(rate - 0.5) < 1e-9


# ---------- B 臂工具面契约 ----------

def test_arm_b_schemas_exist_and_no_auth_tools():
    schemas = ab_run.arm_b_schemas()
    names = [s["name"] for s in schemas]
    assert len(names) == len(set(names)) >= 10
    for t in registry.list_tools():
        if t["name"] in set(names):
            assert not t.get("requires_auth"), t["name"]     # 只读证据面不带授权工具
            assert s_param_ok(t)


def s_param_ok(entry):
    schema = entry.get("inputSchema") or {}
    return isinstance(schema, dict) and schema.get("type") == "object"


def test_exec_tool_unknown_tool_is_structured_error():
    txt = ab_run.exec_tool("__no_such_tool__", {})
    assert '"ok": false' in txt.replace(" ", "").replace('"ok":false', '"ok": false') or "未知工具" in txt
