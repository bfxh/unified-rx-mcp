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

from ide_tools import (  # noqa: E402
    _strip_comments_strings,
    ide_actions,
    ide_complete,
    ide_references,
    ide_rename,
)


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


def test_actions_directory_batch():
    """IDE 增强四：ide_actions 目录批量（多文件聚合 + 上限防护）。"""
    root = tempfile.mkdtemp(prefix="ide_actions_batch_")  # 前缀不含 test（防被当测试目录）
    for rel, content in {
        "a.rs": "fn main() {\n    let x = foo().unwrap();\n}\n",
        "b.py": (
            "def run():\n"
            "    try:\n"
            "        return 1\n"
            "    except ValueError:\n"
            "        pass\n"   # 吞错
        ),
        "c.rs": "// 干净文件\n",
        "d.txt": "ignored 不扫描\n",
    }.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(content)
    r = ide_actions(root)
    assert r["ok"]
    assert r["files_scanned"] == 3, f"应扫 3 个代码文件（忽略 .txt），实际 {r['files_scanned']}"
    keys = list(r["actions_by_file"].keys())
    assert any(k.endswith("a.rs") for k in keys), f"a.rs 应有建议: {keys}"
    assert any(k.endswith("b.py") for k in keys), f"b.py 应有建议: {keys}"
    assert r["total"] >= 2, f"应有 unwrap + 吞错建议，实际 {r['total']}"
    # 单文件模式向后兼容
    r2 = ide_actions(os.path.join(root, "a.rs"))
    assert r2["ok"] and r2["count"] == 1 and "file" in r2
    assert r2["actions"][0]["title"].startswith("unwrap")


def test_actions_directory_limit():
    """批量上限：文件数/总建议数截断不爆炸。"""
    files = {}
    for i in range(60):
        files[f"f{i}.rs"] = f"fn f{i}() {{ let x = v{i}().unwrap(); }}\n"
    root = _tmp_project(files)
    r = ide_actions(root)
    assert r["ok"]
    assert r["files_scanned"] <= 50, f"文件数应被截断: {r['files_scanned']}"
    assert r["truncated"] is True, "超限应标记 truncated"


def test_actions_no_false_positive_on_normal_code():
    p = os.path.join(tempfile.mkdtemp(prefix="ide_actions2_"), "y.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("def ok():\n    return 42\n")
    r = ide_actions(p)
    assert r["ok"] and r["count"] == 0, f"干净代码不应有 action: {r}"


# ── IDE 增强二（2026-08-13）：ide_references 定义/引用区分 + complete 当前文件优先 ──
def test_references_definitions_and_calls():
    root = _tmp_project({
        "lib.rs": (
            "pub fn compute_area(w: f32) -> f32 { w }\n"
            "fn main() {\n"
            "    let a = compute_area(2.0);\n"   # 引用（调用）
            "    // compute_area 注释（排除）\n"
            "}\n"
        ),
    })
    r = ide_references(root, "compute_area")
    assert r["ok"]
    # 定义 1 处（行 1 pub fn）+ 引用 1 处（行 3 调用）
    assert r["definition_count"] == 1, f"定义数: {r['definition_count']}"
    assert r["reference_count"] == 1, f"引用数: {r['reference_count']}"
    assert r["definitions"][0]["line"] == 1
    assert r["references"][0]["line"] == 3


def test_references_no_fakes_from_comments():
    root = _tmp_project({
        "a.py": (
            "def ghost(): pass\n"           # 定义
            "x = ghost()\n"                 # 引用
            "# ghost 注释（排除）\n"
            's = "ghost 字符串"（排除）\n'
        ),
    })
    r = ide_references(root, "ghost")
    assert r["ok"]
    assert r["definition_count"] == 1
    assert r["reference_count"] == 1, f"注释/字符串里的 ghost 不应计入: {r['references']}"


def test_references_unknown_symbol():
    root = _tmp_project({"a.rs": "fn main() {}\n"})
    r = ide_references(root, "nope_symbol")
    assert not r["ok"]


def test_complete_current_file_priority():
    root = _tmp_project({
        "current.rs": "fn helper_local() {}\nfn helper_local2() {}\n",
        "other.rs": "fn helper_remote() {}\n",
    })
    cur = os.path.join(root, "current.rs")
    r = ide_complete(root, cur, "helper")
    assert r["ok"]
    det = r["detailed"]
    # 当前文件符号（helper_local/helper_local2）应排在其他文件符号（helper_remote）前
    assert det[0]["current"] is True and det[1]["current"] is True
    assert det[2]["current"] is False
    assert [d["name"] for d in det[:2]] == ["helper_local", "helper_local2"]


# ── Bug 修复回归（2026-08-13）：跨行块注释行号错位 + Python # 注释 ──
def test_rename_line_numbers_survive_block_comments():
    """跨行块注释不得导致行号错位（实测旧版 9 行文件错位 3 行）。"""
    root = _tmp_project({
        "a.rs": (
            "/*\n"
            " * block comment\n"
            " * across 3 lines\n"
            " */\n"
            "fn main() {\n"
            "    let velocity = 1.0;\n"
            "    // velocity in comment\n"
            "    println!(\"{}\", velocity);\n"
            "}\n"
        ),
    })
    r = ide_rename(root, "velocity", "speed")
    assert r["ok"]
    lines = [x["line"] for x in r["refs"]]
    assert lines == [6, 8], f"行号应精确（块注释后不错位），实际 {lines}"


def test_rename_python_inline_hash_comment_stripped():
    """Python 行内 # 注释里的符号不应被当引用（旧版只剥行首 #）。"""
    root = _tmp_project({
        "a.py": (
            "def run():\n"
            "    x = 1  # velocity in inline comment\n"
            "    y = velocity * 2\n"
            "    return x + y\n"
        ),
    })
    r = ide_rename(root, "velocity", "speed")
    assert r["ok"]
    lines = [x["line"] for x in r["refs"]]
    assert lines == [3], f"行内 # 注释里的 velocity 不应计数，实际 {lines}（引用应在行 3）"


def test_rename_rust_attribute_hash_preserved():
    """Rust 属性 #[...]/#![...] 的 # 不是注释（不得误剥导致行内容错乱）。"""
    code = _strip_comments_strings(
        "#[derive(Debug)]\n#![allow(dead_code)]\nfn main() { let x = 1; }\n"
    )
    lines = code.splitlines()
    assert lines[0].startswith("#[derive"), "Rust 属性行应保留"
    assert lines[1].startswith("#!["), "Rust 内部属性行应保留"
    assert len(lines) == 3, "属性不是注释，行数不得减少"


def test_strip_keeps_line_count_with_triple_quotes():
    """Python 三引号跨行字符串：剥离后行数不变（行号保持）。"""
    code = _strip_comments_strings(
        'def run():\n    s = """multi\nline\nstring"""\n    return s\n'
    )
    assert len(code.splitlines()) == 5, f"三引号跨行字符串剥离不得减行，实际 {len(code.splitlines())}"
    assert "multi" not in code and "string" not in code, "三引号内容应被剥"


# ── IDE 增强三：rename 替换预览 ──
def test_rename_preview_before_after():
    root = _tmp_project({
        "a.rs": "fn main() {\n    let velocity = 1.0;\n    use_velocity(velocity);\n}\n",
    })
    r = ide_rename(root, "velocity", "speed")
    assert r["ok"]
    for ref in r["refs"]:
        assert "before" in ref and "after" in ref, "refs 应带 before/after 预览"
        assert "velocity" in ref["before"]
        assert "speed" in ref["after"]
        # 独立 token 不应残留（use_velocity 里的 velocity 是子串，允许）
        import re as _re
        assert not _re.search(r"\bvelocity\b", ref["after"]), f"after 不应残留独立旧名: {ref}"
