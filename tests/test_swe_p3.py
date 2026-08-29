# -*- coding: utf-8 -*-
"""S23 swe_p3 机械层回归：DSML 残片解析/回收、patch 提取、git apply 校验。"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import swe_p3  # noqa: E402

D = "\uff5c"          # 全角竖线分隔符（实跑出现 ×1~2，runner 正则可变长匹配）


# ---------- parse_dsml ----------

def test_parse_dsml_two_invokes_typed_params():
    s = (f"<{D}DSML{D}tool_calls>\n"
         f'<{D}DSML{D}invoke name="code_search">\n'
         f'<{D}DSML{D}parameter name="query" string="true">def f</{D}DSML{D}parameter>\n'
         f'<{D}DSML{D}parameter name="k" string="false">20</{D}DSML{D}parameter>\n'
         f"</{D}DSML{D}invoke>\n"
         f'<{D}DSML{D}invoke name="fs_read">\n'
         f'<{D}DSML{D}parameter name="path" string="true">a.py</{D}DSML{D}parameter>\n'
         f"</{D}DSML{D}invoke>\n"
         f"</{D}DSML{D}tool_calls>")
    out = swe_p3.parse_dsml(s)
    assert out == [("code_search", {"query": "def f", "k": 20}),
                   ("fs_read", {"path": "a.py"})]


def test_parse_dsml_plain_text_clean():
    assert swe_p3.parse_dsml("normal answer with ```diff block") == []
    assert swe_p3.parse_dsml("") == []
    assert swe_p3.parse_dsml(None) == []


def test_parse_dsml_double_bar_real_format():
    # 回归：实跑数据分隔符是双全角竖线（曾因单条正则导致回收层全程空转）
    s = (f"<<{D}{D}DSML{D}{D}invoke name=\"t\">"
         f"<{D}{D}DSML{D}{D}parameter name=\"q\" string=\"true\">x</"
         f"{D}{D}DSML{D}{D}parameter></{D}{D}DSML{D}{D}invoke>")
    assert swe_p3.parse_dsml(s) == [("t", {"q": "x"})]


def test_parse_dsml_bad_json_stays_string():
    s = (f'<{D}DSML{D}invoke name="t">\n'
         f'<{D}DSML{D}parameter name="x" string="false">not-json</{D}DSML{D}parameter>\n'
         f"</{D}DSML{D}invoke>")
    assert swe_p3.parse_dsml(s) == [("t", {"x": "not-json"})]


# ---------- extract_patch ----------

def test_extract_patch_last_block_wins():
    a = "prose\n```diff\nfirst\n```\nmid\n```diff\nsecond\n```\ntail"
    assert swe_p3.extract_patch(a) == "second\n"


def test_extract_patch_unfenced_diff():
    a = "analysis\ndiff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
    assert swe_p3.extract_patch(a).startswith("diff --git")


def test_extract_patch_empty():
    assert swe_p3.extract_patch("no patch here") == ""
    assert swe_p3.extract_patch("") == ""


def test_extract_patch_keeps_trailing_newline():
    # 回归：strip 掉尾换行会让 git apply 报 corrupt patch（S23 自测咬出）
    a = "```diff\n" + VALID_DIFF.rstrip("\n") + "\n```"
    assert swe_p3.extract_patch(a).endswith(" line3\n")


# ---------- patch_check ----------

VALID_DIFF = (
    "diff --git a/a.txt b/a.txt\n"
    "--- a/a.txt\n"
    "+++ b/a.txt\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+line2b\n"
    " line3\n"
)


@pytest.fixture()
def mini_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    (tmp_path / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    return str(tmp_path)


def test_patch_check_plain_ok(mini_repo):
    ok, strat, err = swe_p3.patch_check(mini_repo, VALID_DIFF)
    assert ok and strat == "plain" and not err


def test_patch_check_p0_fallback(mini_repo):
    # 单段路径 sub/b.txt：默认 -p1 剥成根下 b.txt（不存在）必失败，-p0 命中
    p0 = ("--- sub/b.txt\n+++ sub/b.txt\n@@ -1,3 +1,3 @@\n"
          " line1\n-line2\n+line2b\n line3\n")
    ok, strat, _ = swe_p3.patch_check(mini_repo, p0)
    assert ok and strat == "p0"


def test_patch_check_missing_file_fails_all(mini_repo):
    bad = VALID_DIFF.replace("a/a.txt", "a/nope.txt").replace("b/a.txt", "b/nope.txt")
    ok, strat, err = swe_p3.patch_check(mini_repo, bad)
    assert not ok and strat == "" and err


def test_patch_check_empty_or_no_hunk(mini_repo):
    ok, _, err = swe_p3.patch_check(mini_repo, "")
    assert not ok and err == "empty-or-no-hunk"
    ok, _, err = swe_p3.patch_check(mini_repo, "random text no hunks")
    assert not ok and err == "empty-or-no-hunk"


# ---------- search/replace 机械落地层 ----------

SR_BLOCK = ("path: a.txt\n"
            "<<<<<<< SEARCH\n"
            "line2\n"
            "=======\n"
            "line2b\n"
            ">>>>>>> REPLACE\n")


def test_parse_sr_valid_and_missing_path():
    out = swe_p3.parse_sr("```sr\n" + SR_BLOCK + "```")
    assert out == [("a.txt", "line2", "line2b")]
    assert swe_p3.parse_sr("```sr\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n```") == []
    assert swe_p3.parse_sr("plain answer") == []


def test_apply_sr_applies_generates_git_diff_and_restores(mini_repo):
    applied, fails, diff, fz, grounds = swe_p3.apply_sr(
        mini_repo, [("a.txt", "line2", "line2b")])
    assert applied == 1 and fails == [] and fz == 0
    assert "-line2" in diff and "+line2b" in diff
    # 幂等：diff 取出后仓库必须还原
    assert open(os.path.join(mini_repo, "a.txt"), encoding="utf-8"
                ).read().replace("\r\n", "\n") == "line1\nline2\nline3\n"


def test_apply_sr_fuzzy_recovers_drifted_search(mini_repo):
    # 模型把注释措辞写飘了：精确匹配必败，模糊窗按相似度兜住
    p = os.path.join(mini_repo, "sub", "c.txt")
    open(p, "w", encoding="utf-8", newline="").write(
        "def f():\n    # TODO: later\n    return 1\n")
    subprocess.run(["git", "-C", mini_repo, "add", "-A"], check=True)
    s = "def f():\n    # TODO: afterwards\n    return 1\n"
    r = "def f():\n    # TODO: later, then cleanup\n    return 1\n"
    applied, fails, diff, fz, grounds = swe_p3.apply_sr(
        mini_repo, [("sub/c.txt", s, r)])
    assert applied == 1 and fz == 1 and fails == []
    assert "+    # TODO: later, then cleanup" in diff


def test_apply_sr_not_found_and_ambiguous(mini_repo):
    applied, fails, _, _, _ = swe_p3.apply_sr(mini_repo, [("a.txt", "no-such-line", "x")])
    assert applied == 0 and fails == ["a.txt: search-not-found"]
    p = os.path.join(mini_repo, "sub", "b.txt")
    open(p, "w", encoding="utf-8", newline="").write("dup\nother\ndup\n")
    applied, fails, _, _, _ = swe_p3.apply_sr(mini_repo, [("sub/b.txt", "dup", "x")])
    assert applied == 0 and fails == ["sub/b.txt: search-ambiguous(x2)"]
    # 文件缺失
    applied, fails, _, _, _ = swe_p3.apply_sr(mini_repo, [("no/dir/f.py", "a", "b")])
    assert fails == ["no/dir/f.py: file-not-found"]


def test_run_once_sr_protocol_end_to_end(mini_repo, monkeypatch):
    final = {"choices": [{"message": {
        "content": "fix\n```sr\n" + SR_BLOCK + "```", "tool_calls": None}}]}
    monkeypatch.setattr(swe_p3.AB, "chat", lambda *a, **k: final)
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    ans, _, _, _, mech = swe_p3.run_once("A", inst, {}, "m", 6, mini_repo,
                                         [{"name": "fs_read"}])
    assert mech["protocol"] == "sr"
    assert mech["sr_applied"] == 1 and mech["patch_ok"] is True
    assert "-line2" in mech["candidate_diff"] and "+line2b" in mech["candidate_diff"]


def test_run_once_sr_repair_round_on_bad_search(mini_repo, monkeypatch):
    bad = ("```sr\npath: a.txt\n<<<<<<< SEARCH\nWRONG-CONTEXT\n=======\nx\n"
           ">>>>>>> REPLACE\n```")
    good = "```sr\n" + SR_BLOCK + "```"
    replies = [{"choices": [{"message": {"content": "v1\n" + bad, "tool_calls": None}}]},
               {"choices": [{"message": {"content": "v2\n" + good, "tool_calls": None}}]}]
    monkeypatch.setattr(swe_p3.AB, "chat", lambda *a, **k: replies.pop(0))
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    _, _, _, _, mech = swe_p3.run_once("A", inst, {}, "m", 6, mini_repo,
                                       [{"name": "fs_read"}])
    assert mech["patch_repaired"] is True
    assert mech["patch_ok"] is True and mech["sr_applied"] == 1


def test_run_once_final_dsml_recovered_then_sr(mini_repo, monkeypatch):
    dsml_final = {"choices": [{"message": {
        "content": (f"searching\n<{D}DSML{D}invoke name=\"code_search\">\n"
                    f"<{D}DSML{D}parameter name=\"query\" string=\"true\">line</{D}DSML{D}parameter>\n"
                    f"</{D}DSML{D}invoke>"),
        "tool_calls": None}},]}
    sr_final = {"choices": [{"message": {
        "content": "```sr\n" + SR_BLOCK + "```", "tool_calls": None}}]}
    seq = {"n": 0}

    def fake_chat(ch, model, msgs, tools_schema=None):
        seq["n"] += 1
        return dsml_final if seq["n"] <= 2 else sr_final

    seen_tools = []
    monkeypatch.setattr(swe_p3.AB, "chat", fake_chat)
    monkeypatch.setattr(swe_p3.AB, "exec_tool",
                        lambda n, a: (seen_tools.append(n) or ("[]", 3)))
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    # max_rounds=1：轮内回收一次后强制收线，收线响应仍是 DSML → 终轮回收接管
    _, _, _, trace, mech = swe_p3.run_once("B", inst, {}, "m", 1, mini_repo,
                                           [{"name": "code_search"}])
    assert mech["dsml_final_recovered"] == 1
    assert seen_tools == ["code_search", "code_search"]
    assert mech["sr_applied"] == 1 and mech["patch_ok"] is True


def test_run_once_grounded_authoring_for_blockless_final(mini_repo, monkeypatch):
    prose = {"choices": [{"message": {
        "content": "the fix is trivial, in a.txt obviously.", "tool_calls": None}}]}
    locate = {"choices": [{"message": {"content": "path: a.txt", "tool_calls": None}}]}
    author = {"choices": [{"message": {
        "content": "```sr\n" + SR_BLOCK + "```", "tool_calls": None}}]}
    replies = [prose, locate, author]

    def fake_chat(ch, model, msgs, tools_schema=None):
        return replies.pop(0)

    monkeypatch.setattr(swe_p3.AB, "chat", fake_chat)
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    _, _, _, _, mech = swe_p3.run_once("A", inst, {}, "m", 6, mini_repo,
                                       [{"name": "fs_read"}])
    assert mech["grounded_author"] is True
    assert mech["protocol"] == "sr" and mech["sr_applied"] == 1
    assert mech["patch_ok"] is True


def test_run_once_locate_dsml_recovered(mini_repo, monkeypatch):
    prose = {"choices": [{"message": {"content": "analysis only.", "tool_calls": None}}]}
    dsml_locate = {"choices": [{"message": {
        "content": (f"<{D}DSML{D}invoke name=\"code_search\">\n"
                    f"<{D}DSML{D}parameter name=\"query\" string=\"true\">line</{D}DSML{D}parameter>\n"
                    f"</{D}DSML{D}invoke>"),
        "tool_calls": None}}]}
    locate = {"choices": [{"message": {"content": "path: a.txt", "tool_calls": None}}]}
    author = {"choices": [{"message": {
        "content": "```sr\n" + SR_BLOCK + "```", "tool_calls": None}}]}
    replies = [prose, dsml_locate, locate, author]

    def fake_chat(ch, model, msgs, tools_schema=None):
        return replies.pop(0)

    seen = []
    monkeypatch.setattr(swe_p3.AB, "chat", fake_chat)
    monkeypatch.setattr(swe_p3.AB, "exec_tool",
                        lambda n, a: (seen.append(n) or ("[]", 3)))
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    _, _, _, _, mech = swe_p3.run_once("A", inst, {}, "m", 6, mini_repo,
                                       [{"name": "fs_read"}])
    assert mech["dsml_final_recovered"] == 1 and seen == ["code_search"]
    assert mech["grounded_author"] is True and mech["sr_applied"] == 1


def test_run_once_recovers_dsml_round(mini_repo, monkeypatch):
    dsml_reply = {"choices": [{"message": {
        "content": (f"searching\n<{D}DSML{D}invoke name=\"code_search\">\n"
                    f"<{D}DSML{D}parameter name=\"query\" string=\"true\">line</{D}DSML{D}parameter>\n"
                    f"</{D}DSML{D}invoke>"),
        "tool_calls": None}},]}
    final_reply = {"choices": [{"message": {
        "content": "done\n```diff\n" + VALID_DIFF + "\n```",
        "tool_calls": None}},]}
    seq = {"n": 0}

    def fake_chat(ch, model, msgs, tools_schema=None):
        seq["n"] += 1
        return dsml_reply if seq["n"] == 1 else final_reply

    def fake_exec_tool(name, args):
        assert name == "code_search" and args.get("query") == "line"
        return "[]", 3

    monkeypatch.setattr(swe_p3.AB, "chat", fake_chat)
    monkeypatch.setattr(swe_p3.AB, "exec_tool", fake_exec_tool)
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    ans, tin, tout, trace, mech = swe_p3.run_once(
        "B", inst, {}, "m", 6, mini_repo, [{"name": "code_search"}])
    assert VALID_DIFF in ans
    assert mech["dsml_recovered"] == 1
    assert mech["patch_ok"] is True
    assert mech["patch_strategy"] == "plain"
    assert any(t.get("dsml") for t in trace)


def test_run_once_repair_round_on_bad_patch(mini_repo, monkeypatch):
    bad = VALID_DIFF.replace("a/a.txt", "a/nope.txt").replace("b/a.txt", "b/nope.txt")
    replies = [
        {"choices": [{"message": {"content": "v1\n```diff\n" + bad + "\n```",
                                  "tool_calls": None}}]},
        {"choices": [{"message": {"content": "v2\n```diff\n" + VALID_DIFF + "\n```",
                                  "tool_calls": None}}]},
    ]
    monkeypatch.setattr(swe_p3.AB, "chat", lambda *a, **k: replies.pop(0))
    inst = {"instance_id": "x/y-1", "issue": "i", "gold_patch": ""}
    ans, _, _, _, mech = swe_p3.run_once("A", inst, {}, "m", 6, mini_repo,
                                         [{"name": "fs_read"}])
    assert mech["patch_repaired"] is True
    assert mech["patch_ok"] is True
    assert VALID_DIFF in ans
