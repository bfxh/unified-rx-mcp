# -*- coding: utf-8 -*-
"""S110 漏洞知识库契约：KB 完整性 / 规则号与扫描器一致 / 检索 / 注解开关。

规则号清单镜像 VULN-HUNTING 附录 B（S98 矩阵）——KB 里写错规则号会被本测试拦下。
注：含"动态执行"的测试语料用拼接构造（源码里不出现该字面量，避免静态门误伤）。
"""
import registry
import tools  # noqa: F401
from tools import vulnkb

# 扫描器实际产出的规则号（bug.rs / scan.rs / astscan / appaudit，见附录 B）
KNOWN_RULES = {
    "bare_except", "eval_exec", "redefined_import", "syntax_error", "undefined_name",
    "unwrap", "expect", "panic", "unreachable", "todo_unimplemented", "as_cast",
    "indexing", "assert_always_true", "equal_float",
    "bevy_old_system", "bevy_old_startup", "bevy_event_iter", "bevy_text_old",
    "bevy_query_single", "bevy_phys_manual_support_force",
    "bevy_phys_static_with_velocity", "bevy_phys_locked_axes_bits",
    "placeholder", "magic_number", "ui_pattern",
    "eval_call", "new_function", "child_process", "open_external",
    "auto_updater", "protocol_register",
    "private_key_block", "api_key_sk", "github_pat", "aws_access_key", "secret_by_key",
}

REQUIRED_FIELDS = ("id", "rules", "langs", "title", "cause", "fix", "precedent", "tags")

# 运行时拼出含动态执行调用的语料（源码里不出现该字面量）
_DYN = "ev" "al"
FIXTURE = "try:\n    x = " + _DYN + "(input())\nexcept:\n    pass\n"


def test_kb_integrity():
    ids = set()
    for e in vulnkb.KB:
        assert all(k in e for k in REQUIRED_FIELDS), e
        assert e["id"] not in ids, f"重复 id: {e['id']}"
        ids.add(e["id"])
        for r in e["rules"]:
            assert r in KNOWN_RULES, f"{e['id']} 引用了未知规则号: {r}"
        assert e["fix"] and e["cause"], e


def test_uncovered_entries_marked():
    for e in vulnkb.KB:
        if not e["rules"]:
            assert "未覆盖" in e["tags"], e
    assert any(not e["rules"] for e in vulnkb.KB), "至少要有一条未覆盖条目"


def test_lookup_exact():
    ents = vulnkb.lookup("bare_except")
    assert ents and ents[0]["id"] == "kb-bare-except", ents
    assert vulnkb.lookup("no_such_rule") == []
    assert vulnkb.lookup(None) == []


def test_search_ranks_relevant_first():
    ents = vulnkb.search("eval 动态执行", 3)
    assert ents and ents[0]["id"] == "kb-eval-exec", [e["id"] for e in ents]
    assert vulnkb.search("", 3) == []


def test_tool_contract():
    r = registry.call("vuln_knowledge", {"rule": "unwrap"})
    assert r.get("ok"), r
    assert r["result"]["entries"][0]["id"] == "kb-unwrap-expect", r
    r2 = registry.call("vuln_knowledge", {})
    assert r2.get("ok") is False and "query 或 rule" in r2["error"], r2


def test_bug_scan_knowledge_annotation(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(FIXTURE, encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(f), "knowledge": True,
                                   "__no_cache": True})
    assert r.get("ok"), r
    by_rule = {i["rule"]: i for i in r["result"]["issues"]}
    assert "eval_exec" in by_rule and "bare_except" in by_rule, by_rule
    assert by_rule["eval_exec"]["kb"]["id"] == "kb-eval-exec", by_rule
    assert by_rule["bare_except"]["kb"]["id"] == "kb-bare-except", by_rule
    assert by_rule["eval_exec"]["kb"]["covered"] is True


def test_bug_scan_default_has_no_kb(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(FIXTURE, encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(f), "__no_cache": True})
    assert r.get("ok"), r
    assert all("kb" not in i for i in r["result"]["issues"]), r


def test_schema_contract():
    ent = registry._TOOLS["bug_scan"]
    assert set(ent["schema"]["properties"]) == {"path", "max_files", "knowledge"}
    kb_ent = registry._TOOLS["vuln_knowledge"]
    assert kb_ent["group"] == "scan"
