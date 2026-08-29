# -*- coding: utf-8 -*-
"""S65：IDE 升级回归钉——code_review lens 过滤 + 测试区复杂度跳过 +
ide_batch_edit 跨文件批量替换（dry_run/apply/语法门/白名单）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

AUTH = {"__authorized": True}


# ---------- code_review lens 过滤 ----------

def test_code_review_lens_filter(tmp_path):
    (tmp_path / "a.py").write_text(
        "def f():\n    return eval(u)\n\n\n"
        "def g(u):\n    # TODO 待办\n    return 1\n", encoding="utf-8")
    all_f = registry.call("code_review", {"path": str(tmp_path)})["result"]
    assert all_f["total"] >= 2 and "security" in all_f["by_lens"]
    r = registry.call("code_review", {"path": str(tmp_path), "lens": "security"})
    res = r["result"]
    assert res["lens"] == "security"
    assert all(f["lens"] == "security" for f in res["findings"])
    assert res["by_lens"] == {"security": res["total"]}
    r2 = registry.call("code_review", {"path": str(tmp_path), "lens": "todo"})
    assert r2["result"]["lens"] == "todo"


# ---------- 测试区复杂度跳过 ----------

def test_complexity_skips_cfg_test_region(tmp_path):
    """S65：rust #[cfg(test)] mod 内的长函数（测试夹具）不是产品复杂度。"""
    (tmp_path / "m.rs").write_text(
        "pub fn small() -> u32 { 1 }\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn wheel_def(id: &str) -> ModuleDef {\n"
        + "".join(f"        // fill {i}\n        let x{i} = {i};\n"
                  for i in range(45))
        + "        ModuleDef::default()\n"
        "    }\n"
        "}\n", encoding="utf-8")
    r = registry.call("code_review", {"path": str(tmp_path)})
    cx = [f for f in r["result"]["findings"] if f["lens"] == "complexity"]
    assert cx == [], f"测试夹具函数被当产品复杂度: {cx}"


def test_complexity_skips_python_test_files(tmp_path):
    (tmp_path / "test_app.py").write_text(
        "def test_long_fixture():\n"
        + "".join(f"    x{i} = {i}\n" for i in range(90))
        + "    assert True\n", encoding="utf-8")
    r = registry.call("code_review", {"path": str(tmp_path)})
    cx = [f for f in r["result"]["findings"] if f["lens"] == "complexity"]
    assert cx == []


def test_complexity_still_flags_product_code(tmp_path):
    (tmp_path / "app.py").write_text(
        "def real_hotspot():\n"
        + "".join(f"    x{i} = {i}\n" for i in range(90))
        + "    return x0\n", encoding="utf-8")
    r = registry.call("code_review", {"path": str(tmp_path)})
    cx = [f for f in r["result"]["findings"] if f["lens"] == "complexity"]
    assert len(cx) == 1, "产品代码长函数必须仍被标记"


# ---------- ide_batch_edit ----------

def _proj(tmp_path):
    for name in ("a.py", "sub/b.py"):
        p = tmp_path / name
        p.parent.mkdir(exist_ok=True)
        p.write_text("VERSION = 1\n", encoding="utf-8")
    return tmp_path


EDITS = [{"old_lines": ["VERSION = 1"], "new_lines": ["VERSION = 2"]}]


def test_batch_dry_run_previews_without_write(tmp_path):
    _proj(tmp_path)
    r = registry.call("ide_batch_edit", {**AUTH, "path": str(tmp_path),
                                         "edits": EDITS})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["dry_run"] is True and res["matched"] == 2
    assert res["applied_files"] == 0
    assert all("diff" in f for f in res["files"])
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "VERSION = 1\n"


def test_batch_apply_writes_all(tmp_path):
    _proj(tmp_path)
    r = registry.call("ide_batch_edit", {**AUTH, "path": str(tmp_path),
                                         "edits": EDITS, "apply": True})
    res = r["result"]
    assert res["applied_files"] == 2 and res["dry_run"] is False
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "VERSION = 2\n"
    assert (tmp_path / "sub" / "b.py").read_text(encoding="utf-8") == "VERSION = 2\n"
    assert not list(tmp_path.rglob("*.urxtmp*"))


def test_batch_syntax_gate_skips_only_broken_file(tmp_path):
    """单文件语法门失败只跳过该文件，不挡批次（两条 edit 分别命中好坏文件）。"""
    (tmp_path / "good.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("B = 1\n", encoding="utf-8")
    edits = [{"old_lines": ["A = 1"], "new_lines": ["A = 2"]},
             {"old_lines": ["B = 1"], "new_lines": ["B = 2 :"]}]
    r = registry.call("ide_batch_edit", {**AUTH, "path": str(tmp_path),
                                         "edits": edits, "apply": True})
    res = r["result"]
    assert res["applied_files"] == 1
    assert len(res["errors"]) == 1 and "语法门" in res["errors"][0]["error"]
    assert (tmp_path / "good.py").read_text(encoding="utf-8") == "A = 2\n"
    assert (tmp_path / "bad.py").read_text(encoding="utf-8") == "B = 1\n"


def test_batch_files_whitelist(tmp_path):
    _proj(tmp_path)
    r = registry.call("ide_batch_edit", {**AUTH, "path": str(tmp_path),
                                         "edits": EDITS, "apply": True,
                                         "files": ["a.py"]})
    res = r["result"]
    assert res["matched"] == 1
    assert (tmp_path / "sub" / "b.py").read_text(encoding="utf-8") == "VERSION = 1\n"


def test_batch_requires_auth(tmp_path):
    _proj(tmp_path)
    r = registry.call("ide_batch_edit", {"path": str(tmp_path), "edits": EDITS})
    assert not r["ok"] and "授权" in r["error"]
