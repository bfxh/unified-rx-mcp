"""S140 打点来源契约：mcp=协议分发 / embedded=程序直调；usage_stats 按来源拆分。

口径：server.py 分发线程先 set_request_context(msg_id)（默认 source=mcp）再
registry.call → 打点 src=mcp；call_with_context（测试/嵌入式宿主）→ src=embedded；
脚本不经上下文直接 registry.call → 线程本地无标记 → src=embedded。
改造前的旧记录无 src 字段 → usage_stats 归为 unmarked。
打点落点已被根 conftest 重定向到 tmp（不污染真实 ~/.unified-rx/stats.jsonl）。
"""
import json

import registry
import tools  # noqa: F401
from tools import ops


def _stats_file(monkeypatch, tmp_path):
    f = tmp_path / "stats.jsonl"
    monkeypatch.setattr(ops, "_STATS_FILE", str(f))
    # 覆盖 conftest 的共享落点：本组测试要断言"文件里恰好是这几条"，
    # 必须独享一个空文件（其余测试仍走 conftest 的共享隔离区）
    monkeypatch.setattr(registry, "_stats_path", lambda: str(f))
    return f


def test_record_stats_tags_source_by_context(monkeypatch, tmp_path):
    """有 msg_id（协议分发路径）→ mcp；无上下文（脚本直调）→ embedded。"""
    f = _stats_file(monkeypatch, tmp_path)
    registry.clear_request_context()
    registry.call("breaker_status", {})          # 脚本直调：无上下文
    registry.set_request_context("req-1")        # 协议分发路径（默认 mcp）
    registry.call("breaker_status", {})
    registry.clear_request_context()
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    assert [r["src"] for r in recs] == ["embedded", "mcp"], recs


def test_call_with_context_tags_embedded(monkeypatch, tmp_path):
    f = _stats_file(monkeypatch, tmp_path)
    registry.call_with_context("breaker_status", {}, "req-2")
    recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    assert recs and recs[0]["src"] == "embedded", recs


def test_usage_stats_splits_by_source(monkeypatch, tmp_path):
    f = _stats_file(monkeypatch, tmp_path)
    rows = [
        {"tool": "fs_read", "duration_ms": 1, "ts": 1789380000, "src": "mcp"},
        {"tool": "fs_read", "duration_ms": 1, "ts": 1789380001, "src": "mcp"},
        {"tool": "fs_write", "duration_ms": 2, "ts": 1789380002, "src": "embedded"},
        {"tool": "fs_stat", "duration_ms": 1, "ts": 1789380003},            # 旧记录无 src
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    st = ops.usage_stats(top=5)
    assert st["total_calls"] == 4, st
    assert st["by_source"] == {"mcp": 2, "embedded": 1, "unmarked": 1}, st.get("by_source")
    assert st["freq_top_mcp"][0] == {"tool": "fs_read", "calls": 2}, st.get("freq_top_mcp")
    assert st["freq_top"][0]["calls"] == 2, st["freq_top"]
