# -*- coding: utf-8 -*-
"""S127：CONSOLIDATION §三 P0 拆分与 §二 C1 遍历除重的契约测试。

守住四件事：
1. filewalk 单一实现的行为等价（三个域的 profile：scan/code_review、ide_common、
   ide_deadcode）——含"单文件无条件产出"这一原 scan 兼容语义；
2. 拆分后注册面不变（code_review 仍在，handler 落在 tools.code_review）；
3. 防回流锁：scan.py 不得再长出评审域符号或自建 os.walk；
4. 死代码清理（aci.strip_hint / scan 双死常量）不回潮。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import filewalk  # noqa: E402
from tools import ide_common, ide_deadcode  # noqa: E402


def _w(p, content):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture()
def tree(tmp_path):
    """a.py / c.rs 计额；b.txt 不计；__pycache__ 与 node_modules 整树跳过。"""
    _w(tmp_path / "src" / "a.py", "A = 1\n")
    _w(tmp_path / "src" / "c.rs", "fn main() {}\n")
    _w(tmp_path / "src" / "b.txt", "not code\n")
    _w(tmp_path / "src" / "__pycache__" / "d.py", "D = 1\n")
    _w(tmp_path / "src" / "node_modules" / "e.py", "E = 1\n")
    return tmp_path


# ---------- 1. filewalk 行为等价 ----------

def test_is_code_file_table():
    assert filewalk.is_code_file("x.py") and filewalk.is_code_file("x.PY")
    assert filewalk.is_code_file("x.rs") and filewalk.is_code_file("x.c")
    assert not filewalk.is_code_file("x.txt") and not filewalk.is_code_file("x")


def test_iter_code_files_profile(tree):
    got = {os.path.basename(p) for p in filewalk.iter_code_files(tree, 100)}
    assert got == {"a.py", "c.rs"}, got


def test_iter_code_files_count_only_matches(tree):
    """max_files 只计产出项：上限 1 时恰好 1 项（旧 scan 语义）。"""
    got = list(filewalk.iter_code_files(tree, 1))
    assert len(got) == 1 and os.path.basename(got[0]) in ("a.py", "c.rs")


def test_single_file_unconditional(tree):
    """file_ok=True 时单文件无条件产出（原 scan._iter_files 兼容语义，不改判）。"""
    fp = str(tree / "src" / "b.txt")
    assert list(filewalk.iter_code_files(fp, 10, file_ok=True)) == [fp]


def test_missing_path_yields_nothing(tmp_path):
    assert list(filewalk.iter_code_files(tmp_path / "nope", 10)) == []


def test_ide_common_profile_and_extra_skip(tmp_path):
    _w(tmp_path / "pkg" / "mod.py", "M = 1\n")
    _w(tmp_path / "pkg" / "notes.md", "# n\n")
    _w(tmp_path / "pkg" / "skipme" / "s.py", "S = 1\n")
    plain = {os.path.basename(p) for p in ide_common._iter_files(tmp_path, 100)}
    assert plain == {"mod.py", "s.py"}, plain      # skipme 非默认跳过集
    skipped = {os.path.basename(p)
               for p in ide_common._iter_files(tmp_path, 100, skip_dirs=("skipme",))}
    assert skipped == {"mod.py"}, skipped          # extra skip 生效


def test_deadcode_profile_venv_and_py_only(tmp_path):
    # S135：ide_dead_code 原生化后 _walk_py 迁 Rust（rex-ide deadcode）——
    # C1b 语义改为**工具级**断言（venv 跳过 / 非 .py 不计）：更强也更稳。
    _w(tmp_path / "app" / "x.py", "def lone():\n    return 1\n")
    _w(tmp_path / "app" / "y.txt", "y\n")
    _w(tmp_path / "app" / "venv" / "z.py", "def zz():\n    return 2\n")
    r = registry.call("ide_dead_code", {"path": str(tmp_path), "__no_cache": True})
    assert r.get("ok"), r
    res = r["result"]
    assert res["files_scanned"] == 1, res          # venv/y.txt 都不计
    assert res["defs_total"] == 1, res             # 只有 x.py 的 lone
    assert {d["name"] for d in res["dead"]} == {"lone"}, res


# ---------- 2. 防回流锁 ----------

def _src(name):
    with open(os.path.join(ROOT, "tools", name), encoding="utf-8") as f:
        return f.read()


def test_single_os_walk_implementation():
    """遍历三件套只允许 filewalk 持有 os.walk；另两家必须零。"""
    assert _src("filewalk.py").count("os.walk(") == 1
    assert "os.walk(" not in _src("ide_common.py")
    assert "os.walk(" not in _src("ide_deadcode.py")


def test_scan_no_longer_hosts_review_domain():
    src = _src("scan.py")
    # docstring 可提其名（历史注释保留），定义/赋值体必须退役
    for needle in ("def code_review", "def _symbol_spans", "def _review_file",
                   "_PLACEHOLDER_WORDS =", "_RE_FUNC_START ="):
        assert needle not in src, f"scan.py 回流：{needle}"
    cr = _src("code_review.py")
    for needle in ("def code_review", "def _symbol_spans", "def _review_file"):
        assert needle in cr, f"code_review.py 缺 {needle}"


# ---------- 3. 注册面契约 ----------

def test_registry_surface_unchanged():
    assert registry.tool_count() == 71
    entry = registry._TOOLS["code_review"]
    assert entry["handler"].__module__ == "tools.code_review"
    assert entry["group"] == "scan"


def test_dead_code_cleanup_sticks():
    from tools import aci
    assert not hasattr(aci, "strip_hint")


def test_c1b_filescan_neardupes_delegate_to_filewalk(tmp_path):
    """S129/C1b：filescan/neardupes 的 walk 收敛到 filewalk 单一实现——
    每层排序语义保留、neardupes 截断如实（恰好上限不标截断）。"""
    from tools import filescan, neardupes
    assert "os.walk(" not in _src("filescan.py")
    assert "os.walk(" not in _src("neardupes.py")
    for name in ("z.py", "a.py", "m.py"):
        _w(tmp_path / "sub" / name, "X = 1\n")
    _w(tmp_path / "venv" / "hidden.py", "H = 1\n")

    got = filescan._walk(str(tmp_path), 100)
    names = [os.path.basename(p) for p in got]
    assert "hidden.py" not in names, names        # venv 跳过
    assert names == sorted(names), names          # 每层排序语义
    assert len(filescan._walk(str(tmp_path), 2)) == 2

    two, trunc2 = neardupes._walk(str(tmp_path), 2)
    assert len(two) == 2 and trunc2 is True       # 有更多 → 如实标截断
    allf, trunc_all = neardupes._walk(str(tmp_path), 100)
    assert len(allf) == 3 and trunc_all is False  # 恰好全量不标截断
    exact, trunc_exact = neardupes._walk(str(tmp_path), 3)
    assert len(exact) == 3 and trunc_exact is False


# ---------- 4. code_review 功能烟测（拆分后真跑）----------

def test_code_review_smoke(tmp_path):
    # 安全透镜走"危险调用"分支（os.system），凭据分支已由 test_s44 覆盖
    _w(tmp_path / "m.py", "import os\nos.system(cmd)\n# TODO: later\n")
    r = registry.call("code_review", {"path": str(tmp_path)})
    assert r.get("ok"), r
    res = r["result"]
    assert res["mode"] == "file" and res["files"] >= 1
    lenses = {f["lens"] for f in res["findings"]}
    assert "security" in lenses and "todo" in lenses, lenses
    # lens 过滤面同锁
    r2 = registry.call("code_review", {"path": str(tmp_path), "lens": "todo"})
    assert r2.get("ok") and {f["lens"] for f in r2["result"]["findings"]} == {"todo"}
