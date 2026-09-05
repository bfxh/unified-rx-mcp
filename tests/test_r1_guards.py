# -*- coding: utf-8 -*-
"""R1 系统性回归门：S52 错绑 / S55 缺导入 两类事故的 AST 级全局守卫。

历史教训（三次同源静默死亡，全靠 except Exception 吞 NameError）：
- S52 ide_diag @tool 错绑 helper
- S55 ide_diag 缺 import registry → LSP+clippy 信号全空
- S55 scan.py code_review 的 bug_scan 透镜同款缺导入 → S44 起从未运行
"""
import ast
import os
import pathlib
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools  # noqa: E402,F401  注册全部
import registry  # noqa: E402


def test_all_tool_handlers_named_after_tool():
    """每个 @tool("x") 必须绑在同名函数上——错绑即在此处爆，不等运行时。"""
    bad = {name: entry["handler"].__name__
           for name, entry in registry._TOOLS.items()
           if entry["handler"].__name__ != name}
    assert not bad, f"工具与 handler 错绑: {bad}"


def test_no_registry_use_without_import():
    """tools/*.py 里凡 Load 名字 registry 必须有 import registry 绑定。

    AST 级（不吃注释假阳性）：from registry import tool 不绑定 registry 名字，
    不可替代 import registry。"""
    gaps = []
    for py in pathlib.Path(ROOT, "tools").glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        imported = any(
            isinstance(n, ast.Import) and any(a.name == "registry" for a in n.names)
            for n in ast.walk(tree))
        uses = [n for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "registry"
                and isinstance(n.ctx, ast.Load)]
        if uses and not imported:
            gaps.append(f"{py.name}: {len(uses)} 处引用无导入")
    assert not gaps, f"registry 使用但未导入（NameError 会被 except 吞成静默死亡）: {gaps}"


def test_code_review_bugscan_lens_alive(tmp_path):
    """S55 事故的功能级回归：bug_scan 透镜必须真的产出发现。

    用 bare_except（bug_scan 的 AST 规则，S83 起实现在 rust/src/bug.rs）——
    eval/exec 只在 generic/security 透镜，python 文件不走，选它会假阴性。"""
    (tmp_path / "evil.py").write_text(
        "def f():\n    try:\n        x = 1\n    except:\n        pass\n",
        encoding="utf-8")
    r = registry.call("code_review", {"path": str(tmp_path)})
    assert r["ok"], r.get("error")
    lenses = {f["lens"] for f in r["result"]["findings"]}
    assert "bug_scan" in lenses, \
        "bug_scan 透镜无输出——多半又是静默死亡（registry 缺导入类）"
