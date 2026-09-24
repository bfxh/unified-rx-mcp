"""S172：智能体归因 + 账本量化/保留期清理的回归测试。

四锁（都含真判据，防"永远绿的摆设"）：
1. 打点带 agent 维度（set_agent("X") → 记录 agent=X；未登记 → null）；
2. 维护把**超期**记录并入 rollup（同键累加）并从原始账本删除——删原始不丢总账；
3. **再次维护继续合并**（同键 counts 相加，不另起行）——"量化可持续合并"的契约；
   并钉住一个实现陷阱：rollup 是多行 pretty JSON，按行读会静默清空旧账（实锤后已修）。
4. usage_audit 报告：近窗口按 agent 归因 + 全期按 rollup 归因 + agents 维度出现在 usage_stats。
"""
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import usage_audit

import registry  # noqa: E402  （ruff 对 sys.path 改动后的 import 不报 E402，无需 noqa）
import tools.ops as ops

NOW = 1_800_000_000
WEEK = 7 * 86400


def _rec(agent, tool, ts, ms=10):
    return {"tool": tool, "duration_ms": ms, "ts": ts, "src": "mcp", "agent": agent}


@pytest.fixture
def stats_env(tmp_path, monkeypatch):
    """stats.jsonl / rollup 全部重定向到 tmp（不污染真实账本）。"""
    stats = tmp_path / "stats.jsonl"
    monkeypatch.setattr(ops, "_STATS_FILE", str(stats))
    monkeypatch.setattr(registry, "_stats_path", lambda: str(stats))
    rollup = tmp_path / "stats-rollup.json"
    return stats, rollup


def test_record_stats_carries_agent(stats_env):
    stats, _ = stats_env
    registry.set_agent("Zed")
    try:
        registry._record_stats("fs_read", 5)
    finally:
        registry.set_agent(None)
    registry._record_stats("fs_read", 5)            # 未登记 → agent null
    recs = ops._load_jsonl(str(stats))
    assert [r.get("agent") for r in recs] == ["Zed", None], recs


def test_maintenance_prunes_into_rollup_and_keeps_recent(stats_env):
    stats, rollup = stats_env
    old_wk = time.strftime("%G-W%V", time.gmtime(NOW - 2 * WEEK))
    lines = [
        _rec("Zed", "fs_read", NOW - 2 * WEEK, 11),     # 超期 → 进 rollup
        _rec("Zed", "fs_read", NOW - 2 * WEEK, 9),      # 超期 → 同键累加（calls=2, ms=20）
        _rec("Yan", "code_search", NOW - 2 * WEEK, 30),  # 超期，不同 agent|tool
        _rec("Zed", "fs_read", NOW - 100, 5),           # 保留期内 → 留原始
    ]
    stats.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
                     encoding="utf-8")
    m = ops.stats_maintenance(retention_days=7, rollup_path=str(rollup), now=NOW)
    assert m["pruned"] == 3 and m["kept"] == 1, m
    # 原始账本只剩保留期内那条
    left = ops._load_jsonl(str(stats))
    assert len(left) == 1 and left[0]["ts"] == NOW - 100, left
    # 总账：同键累加，删了原始也查得到
    doc = json.loads(rollup.read_text(encoding="utf-8"))
    keyed = {e["key"]: e for e in doc["merged"]}
    assert (keyed[f"{old_wk}|Zed|fs_read"]["calls"],
            keyed[f"{old_wk}|Zed|fs_read"]["ms"]) == (2, 20), keyed
    assert (keyed[f"{old_wk}|Yan|code_search"]["calls"],
            keyed[f"{old_wk}|Yan|code_search"]["ms"]) == (1, 30), keyed


def test_maintenance_merges_subsequent_runs(stats_env):
    """"量化可持续合并"：再次维护把新的超期记录**并进同一本账**（同键相加，不另起行）。"""
    stats, rollup = stats_env
    stats.write_text(json.dumps(_rec("Zed", "fs_read", NOW - 2 * WEEK, 11)) + "\n",
                     encoding="utf-8")
    ops.stats_maintenance(retention_days=7, rollup_path=str(rollup), now=NOW)
    # 一周后又攒了一条超期（同 agent|tool；ISO 周可能不同，但同键必然累加）
    stats.write_text(json.dumps(_rec("Zed", "fs_read", NOW - WEEK - 100, 7)) + "\n",
                     encoding="utf-8")
    ops.stats_maintenance(retention_days=7, rollup_path=str(rollup), now=NOW)
    doc = json.loads(rollup.read_text(encoding="utf-8"))
    zed = [e for e in doc["merged"] if e["key"].endswith("|Zed|fs_read")]
    assert sum(e["calls"] for e in zed) == 2, doc
    assert sum(e["ms"] for e in zed) == 18, doc


def test_usage_audit_report_and_usage_stats_agents(stats_env, capsys):
    stats, rollup = stats_env
    stats.write_text(
        json.dumps(_rec("Zed", "fs_read", NOW - 100, 5)) + "\n" +
        json.dumps(_rec("Zed", "fs_read", NOW - 200, 7)) + "\n" +
        json.dumps(_rec("Yan", "code_search", NOW - 300, 40)) + "\n",
        encoding="utf-8")
    rc = usage_audit.main(["--rollup", str(rollup), "--days", "7", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["recent"]["agents"] == 2, out
    per = {r["agent"]: r for r in out["recent"]["per_agent"]}
    assert per["Zed"]["calls"] == 2 and per["Zed"]["top_tools"][0]["tool"] == "fs_read", per
    assert per["Yan"]["calls"] == 1, per
    # usage_stats 也带 agents 维度（附加式，不破坏既有键）
    r = ops.usage_stats()
    assert r["agents"]["distinct"] == 2, r
    assert {"agent": "Zed", "calls": 2} in r["agents"]["top"], r
