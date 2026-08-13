"""test_ide_baseline.py — IDE 工具 R0 基线测试（IDE_ENHANCE_PLAN R0）。

覆盖：
  1. 扩展定义构建后 cae_ 13 工具注册
  2. 关键工具 smoke（aether_probe/change_impact/code_context/lesson_recall/position_convert/file_dedup）
  3. R0 修复回归：lsp_query 后缀自动推断（.rs→rust）
  4. R0 修复回归：lsp_edit_merge 类型校验（str 输入不炸）
  5. semantic preset 完整性（3 个 preset 步数）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

TEST_FILE = r"D:\开发\VoxelForge-Nexus\crates\nexus_app\src\terrain.rs"
REPO = r"D:\开发\VoxelForge-Nexus"


def _call_ext(name, args):
    return server._call_ext(name, args)[0].text


def _build():
    server._ext_definitions()


def test_ext_defs_built():
    """构建后 cae_ 13 工具注册。"""
    _build()
    cae = [k for k in server._EXT_DEFS if k.startswith("cae_")]
    assert len(cae) == 13, f"cae 工具数 {len(cae)} != 13"


def test_aether_probe_smoke():
    _build()
    t = _call_ext("cae_aether_probe", {"repo_path": REPO})
    d = json.loads(t)
    assert d["ok"] is True


def test_change_impact_smoke():
    _build()
    t = _call_ext("cae_change_impact", {
        "repo_path": REPO,
        "changed_files": ["crates/nexus_app/src/terrain.rs"],
    })
    d = json.loads(t)
    assert d["ok"] is True
    assert len(d.get("results", [])) == 1


def test_code_context_smoke():
    _build()
    t = _call_ext("cae_code_context", {"path": TEST_FILE, "cursor_line": 40})
    d = json.loads(t)
    assert d["ok"] is True


def test_lesson_recall_smoke():
    _build()
    t = _call_ext("cae_lesson_recall", {"task_description": "地形生成"})
    d = json.loads(t)
    assert d["ok"] is True


def test_position_convert_smoke():
    _build()
    t = _call_ext("cae_lsp_position_convert", {"path": TEST_FILE, "line": 10, "col": 5})
    d = json.loads(t)
    assert d["ok"] is True


def test_file_dedup_smoke():
    _build()
    t = _call_ext("cae_file_dedup_state", {"path": TEST_FILE})
    d = json.loads(t)
    assert d["ok"] is True


def test_lsp_query_suffix_inference():
    """R0 修复：.rs 后缀自动推断 rust（不传 language_id）。"""
    _build()
    t = _call_ext("cae_lsp_query", {"path": TEST_FILE, "request": "hover", "line": 1})
    d = json.loads(t)
    # 语言应推断为 rust（错误信息是 LSP 层的，不是"不支持的语言"）
    assert "不支持的语言" not in t, f"后缀推断失败: {t[:120]}"
    assert d.get("language") == "rust"


def test_lsp_edit_merge_str_input():
    """R0 修复：edits 传字符串 '[]' 不炸，正确解析。"""
    _build()
    t = _call_ext("cae_lsp_edit_merge", {"edits": "[]"})
    d = json.loads(t)
    assert d["ok"] is True, f"str 输入失败: {t[:120]}"
    assert d["merged_count"] == 0


def test_lsp_edit_merge_list_merge():
    """相邻编辑合并正常。"""
    _build()
    edits = [
        {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}}, "text": "aaa"},
        {"range": {"start": {"line": 1, "character": 5}, "end": {"line": 1, "character": 8}}, "text": "bbb"},
    ]
    t = _call_ext("cae_lsp_edit_merge", {"edits": edits})
    d = json.loads(t)
    assert d["ok"] is True
    assert d["merged_count"] == 1  # 相邻合并为 1
    assert d["merged"][0]["text"] == "aaabbb"


def test_semantic_presets_complete():
    """semantic preset 步数完整（4/3/5）。"""
    expected = {"semantic_before": 4, "semantic_after": 3, "semantic_edit": 5}
    for name, steps in expected.items():
        got = server._PIPELINE_PRESETS.get(name, [])
        assert len(got) == steps, f"{name} 步数 {len(got)} != {steps}"
