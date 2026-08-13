"""test_ide_tools.py — IDE 工具强度增强测试（2026-08-13）。

覆盖：
  1. ide_complete：注释/字符串假符号排除 + 声明优先排序 + detailed 标注
  2. ide_rename：exclude_comments 排除注释/字符串内的引用
  3. ide_actions：TODO/FIXME 未完成标记 + 空 except 吞错检测
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ide_tools import ide_actions, ide_complete, ide_rename  # noqa: E402


def _tmp_project(files: dict[str, str]) -> str:
    """建临时项目（{相对路径: 内容}），返回 root。"""
    root = tempfile.mkdtemp(prefix="ide_tools_test_")
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return root


# ── ide_complete ──
def test_complete_excludes_comment_string_fakes():
    root = _tmp_project({
        "a.rs": "fn main() {\n    // helper_foo 在注释里\n    let s = \"helper_bar\";\n    helper_baz();\n}\n",
        "b.rs": "fn other() {}\n",
    })
    r = ide_complete(root, os.path.join(root, "a.rs"), "helper")
    assert r["ok"]
    # 注释里的 helper_foo 与字符串里的 helper_bar 不应出现；helper_baz 是真调用
    names = r["items"]
    assert "helper_baz" in names, f"真符号应被补全: {names}"
    assert "helper_foo" not in names, f"注释里的假符号不应出现: {names}"
    assert "helper_bar" not in names, f"字符串里的假符号不应出现: {names}"


def test_complete_decl_priority_and_detailed():
    root = _tmp_project({
        "lib.rs": "pub fn compute_area(w: f32) -> f32 { w }\nfn compute_speed() {}\n",
    })
    r = ide_complete(root, os.path.join(root, "lib.rs"), "compute")
    assert r["ok"]
    assert "compute_area" in r["items"] and "compute_speed" in r["items"]
    # detailed 带 kind=decl（行首 pub fn 声明）
    det = {d["name"]: d for d in r["detailed"]}
    assert det["compute_area"]["kind"] == "decl"
    assert det["compute_area"]["file"].endswith("lib.rs")
    assert det["compute_area"]["line"] >= 1


# ── ide_rename ──
def test_rename_excludes_comments_and_strings():
    root = _tmp_project({
        "a.rs": (
            "fn main() {\n"
            "    let velocity = 1.0;\n"          # 真引用
            "    // velocity 注释引用（应排除）\n"
            "    let s = \"velocity 字符串\";\n"   # 字符串（应排除）
            "    println!(\"{}\", velocity);\n"    # 真引用（在字符串模板外）
            "}\n"
        ),
    })
    r = ide_rename(root, "velocity", "speed")
    assert r["ok"]
    refs = r["refs"]
    # 只应有 2 个真引用（let 定义行 2 + println 调用行 5）——注释与字符串行不计数
    code_lines = [x["line"] for x in refs]
    assert len(refs) == 2, f"应只报代码面引用，实际 {len(refs)} 条: {refs}"
    assert 2 in code_lines and 5 in code_lines, f"行号不符: {code_lines}"


def test_rename_no_refs_when_only_in_comments():
    root = _tmp_project({
        "a.rs": "// ghost_symbol 只在注释里\n/* ghost_symbol 块注释 */\n",
    })
    r = ide_rename(root, "ghost_symbol", "real")
    assert not r["ok"], "仅注释/字符串里的符号不应有引用（避免误改注释）"
    assert "未找到" in r["error"]


# ── ide_actions ──
def test_actions_todo_and_empty_except():
    p = os.path.join(tempfile.mkdtemp(prefix="ide_actions_"), "x.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(
            "def run():\n"
            "    # TODO: 实现重试\n"
            "    try:\n"
            "        return 1\n"
            "    except ValueError:\n"
            "        pass\n"     # 空 except 吞错
        )
    r = ide_actions(p)
    assert r["ok"]
    kinds = {a["title"]: a for a in r["actions"]}
    assert any("TODO" in t for t in kinds), f"TODO 应被检测: {list(kinds)}"
    assert any("except" in t and "吞错" in t for t in kinds), (
        f"空 except 吞错应被检测: {list(kinds)}"
    )


def test_actions_no_false_positive_on_normal_code():
    p = os.path.join(tempfile.mkdtemp(prefix="ide_actions2_"), "y.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("def ok():\n    return 42\n")
    r = ide_actions(p)
    assert r["ok"] and r["count"] == 0, f"干净代码不应有 action: {r}"
