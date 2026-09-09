# -*- coding: utf-8 -*-
"""S113 极致门禁（workflow.md 原则 8）：把"看似不关联也会被关联"变成机器检查。

四类门：
1. 文档计数一致性——README / skills/README / PANORAMA 里的工具数与分组数
   必须等于 registry 实际值（本会话 README 停更 80 轮就是这么漏掉的）；
2. 工具入文档**双向**——在册工具必须出现在 skills/*.md（旧 SKILLS_DOCS 只查
   单向，漏了 code_coverage/module_stability/ide_health_trend 三个）；
3. 模块与函数尺寸——默认模块化：tools ≤900 行、rust/src ≤3200 行、函数 ≤200 行
   （当前实测上限 tools/lsp.py 850、pyast.rs 2995、ide_lsp 168 行）；
4. 分组合法 + 检查器自检（impact_check 能跑并输出 JSON）。
5. 版本锁步（S117 补）——server.py / Cargo.toml / Cargo.lock / README 头部四处
   必须同版本（S116 漏更 README 版本头，四道门禁全绿也没抓到，故入机器门）。
6. clippy 零告警（S121 补）——`cargo clippy --all-targets -- -D warnings` 必须过
   （S119/S120 立"vs 原生 Rust"门后，Rust 侧告警从 155 处清到 0；cargo 不可用
   或 clippy 组件缺失 → skip，不假装通过）。
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

import registry
import tools  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN_GROUPS = {"fs", "scan", "ide", "search", "guard", "learn", "ops",
                "game", "engine", "attack", "appaudit", "meta"}

# 尺寸上限（超限先拆分再谈功能；上调需在 workflow.md 原则 8 写明理由）
_MAX_TOOL_LINES = 900
_MAX_RS_LINES = 3200
_MAX_FUNC_LINES = 200


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _groups():
    g = {}
    for name, ent in registry._TOOLS.items():
        g.setdefault(ent.get("group", "?"), []).append(name)
    return g


def test_readme_counts_match_registry():
    readme = _read("README.md")
    n = registry.tool_count()
    pats = [r"\*\*当前 v[\d.]+（S\d+）\*\*：(\d+) 工具 / 12 域",
            r"## 工具面（12 域 · (\d+) 工具）",
            r"\*\*(\d+) 个组合工具 / 12 域\*\*"]
    for pat in pats:
        m = re.search(pat, readme)
        assert m, f"README 缺少计数模式: {pat}"
        assert int(m.group(1)) == n, f"README 计数 {m.group(1)} != registry {n}"


def test_panorama_counts_match_registry():
    pan = _read("spec/PANORAMA.md")
    n = registry.tool_count()
    m = re.search(r"\*\*工具面 (\d+)/12 组\*\*", pan)
    assert m and int(m.group(1)) == n, f"PANORAMA 工具数不一致: {m and m.group(1)} != {n}"
    m2 = re.search(r"（selftest 口径）：(.+?)。", pan, re.S)
    assert m2, "PANORAMA 缺分组清单"
    pairs = dict(re.findall(r"(\w+)\((\d+)\)", m2.group(1)))
    real = _groups()
    assert set(pairs) == set(real), f"分组集合不一致: {set(pairs) ^ set(real)}"
    for k, v in real.items():
        assert int(pairs[k]) == len(v), f"{k} 计数 {pairs[k]} != {len(v)}"


def test_skills_readme_domain_counts():
    s = _read("skills/README.md")
    rows = dict(re.findall(r"^\| (\w+) \| \[[^\]]+\]\([^)]+\) \| (\d+) \|",
                           s, re.M))
    real = _groups()
    assert set(rows) == set(real), f"skills/README 域集合不一致: {set(rows) ^ set(real)}"
    for k, v in real.items():
        assert int(rows[k]) == len(v), f"skills/README {k} 计数 {rows[k]} != {len(v)}"


def test_every_tool_documented_bidirectional():
    docs = "".join(_read(f"skills/{f}") for f in os.listdir(
        os.path.join(ROOT, "skills")) if f.endswith(".md"))
    missing = sorted(n for n in registry._TOOLS if n not in docs)
    assert not missing, f"在册工具未进 skills 文档: {missing}"


def test_groups_known():
    unknown = set(_groups()) - KNOWN_GROUPS
    assert not unknown, f"出现未知分组: {unknown}"


def test_module_size_gate():
    over = []
    tools_dir = os.path.join(ROOT, "tools")
    for fn in sorted(os.listdir(tools_dir)):
        if not fn.endswith(".py"):
            continue
        n = sum(1 for _ in open(os.path.join(tools_dir, fn), encoding="utf-8"))
        if n > _MAX_TOOL_LINES:
            over.append(f"tools/{fn}={n}>{_MAX_TOOL_LINES}")
    rs_dir = os.path.join(ROOT, "rust", "src")
    for fn in sorted(os.listdir(rs_dir)):
        if not fn.endswith(".rs"):
            continue
        n = sum(1 for _ in open(os.path.join(rs_dir, fn), encoding="utf-8"))
        if n > _MAX_RS_LINES:
            over.append(f"rust/src/{fn}={n}>{_MAX_RS_LINES}")
    assert not over, "模块超尺寸（先拆分再谈功能）: " + "; ".join(over)


def test_function_size_gate():
    over = []
    tools_dir = os.path.join(ROOT, "tools")
    for fn in sorted(os.listdir(tools_dir)):
        if not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(tools_dir, fn), encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ln = (node.end_lineno or node.lineno) - node.lineno + 1
                if ln > _MAX_FUNC_LINES:
                    over.append(f"{fn}:{node.name}={ln}>{_MAX_FUNC_LINES}")
    assert not over, "函数超尺寸（先拆分再谈功能）: " + "; ".join(over)


def test_version_lockstep_four_faces():
    """S117 补：版本锁步四处一致（S116 漏更 README 版本头，四道门禁全绿也没抓到）。"""
    srv = _read("server.py")
    m = re.search(r'SERVER_VERSION = "([\d.]+)"', srv)
    assert m, "server.py 缺 SERVER_VERSION"
    ver = m.group(1)
    cargo = _read("rust/Cargo.toml")
    m2 = re.search(r'^version = "([\d.]+)"', cargo, re.M)
    assert m2 and m2.group(1) == ver, f"Cargo.toml {m2 and m2.group(1)} != server {ver}"
    lock = _read("rust/Cargo.lock")
    m3 = re.search(r'name = "unified-rx-rs"\nversion = "([\d.]+)"', lock)
    assert m3 and m3.group(1) == ver, f"Cargo.lock {m3 and m3.group(1)} != server {ver}"
    m4 = re.search(r"\*\*当前 v([\d.]+)（S\d+）\*\*", _read("README.md"))
    assert m4 and m4.group(1) == ver, f"README 版本头 {m4 and m4.group(1)} != server {ver}"


def test_rust_clippy_clean():
    """S121 门：Rust 侧 clippy 零告警（含测试目标）。

    S119/S120 立了"vs 原生 Rust"门后，Rust 代码成为性能基线本体，其告警不能再
    当噪音：155 处 → 0（86 处 collapsible_if 等机器可修 + 38 处手工清）。
    cargo/clippy 不可用 → skip（与 exe 缺失同纪律：不假装通过）。
    """
    if shutil.which("cargo") is None:
        pytest.skip("cargo 不可用")
    cp = subprocess.run(["cargo", "clippy", "--all-targets", "--", "-D", "warnings"],
                        capture_output=True, cwd=os.path.join(ROOT, "rust"),
                        timeout=900, shell=False)
    out = ((cp.stdout or b"") + (cp.stderr or b"")).decode("utf-8", "replace")
    if cp.returncode != 0 and "no such command" in out:
        pytest.skip("clippy 组件未安装（rustup component add clippy）")
    assert cp.returncode == 0, f"clippy 告警未清:\n{out[-2000:]}"


def test_impact_check_runs_and_emits_json(tmp_path):
    """跨面检查器自检：能跑、末行是 JSON、六类面齐全。"""
    script = os.path.join(ROOT, "bench", "impact_check.py")
    cp = subprocess.run([sys.executable, script, "tools/scan.py"],
                        capture_output=True, timeout=120, shell=False)
    out = (cp.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    assert out, cp.stderr
    data = json.loads(out[-1])
    for key in ("files", "modules", "reverse_imports", "tests", "docs",
                "counts", "fixtures", "gate_asserts", "run"):
        assert key in data, f"impact_check 输出缺 {key}"
    assert data["tests"], "改动 tools/scan.py 应命中测试面"
