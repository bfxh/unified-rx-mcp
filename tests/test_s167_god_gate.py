# -*- coding: utf-8 -*-
"""S167：上帝对象门（scripts/god_gate.py）的守门测试。

按本仓纪律「**门真在扫吗，要用金丝雀证明**」——这里锁四件事：
  1. 基线在、门绿（棘轮位）；
  2. **根级文件在扫描面内**（首版 fnmatch 的 `**/x` 不匹配根级 `x` ⇒ server.py/registry.py
     被静默跳过 —— 靠"基线里查不到 server.py"才发现；这条测试就是那次的补丁）；
  3. 真门验证：造一个超阈文件 ⇒ 必须判红（不是永远绿）；
  4. 棘轮语义：基线里的值变大 ⇒ 判红（不许变胖）。
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "god_gate.py"


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-X", "utf8", str(GATE), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(cwd), shell=False, timeout=600)


def _load_gate():
    spec = importlib.util.spec_from_file_location("god_gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_baseline_exists_and_gate_green():
    assert (ROOT / "god-baseline.json").is_file(), "缺 god-baseline.json——先跑 --write-baseline"
    cp = _run("--root", ".", "--top", "0")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "GOD-GATE OK" in cp.stdout, cp.stdout


def test_root_level_files_are_in_scope():
    """金丝雀：根级文件必须被扫到（否则门有盲区，且是静默的）。"""
    gg = _load_gate()
    cfg = gg.load_cfg(ROOT, None)
    for rel in ("server.py", "registry.py", "conftest.py"):
        assert gg.included(rel, cfg), f"根级 {rel} 落在扫描面之外——门有盲区"
    assert gg.included("bench/cli_bench.py", cfg), "bench/ 下的真代码应在扫描面内"
    assert not gg.included("bench/manual_snaps/V3_26_head__ui.rs", cfg), \
        "快照样本（别项目的代码）不该进本仓的扫描面"


def test_gate_red_on_new_oversized_file(tmp_path):
    (tmp_path / "god.gate.json").write_text(json.dumps({"max_file_lines": 800}), encoding="utf-8")
    (tmp_path / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(900)) + "\n",
                                     encoding="utf-8")
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode != 0, "超阈文件没判红——假门\n" + cp.stdout
    assert "GOD-GATE FAIL" in cp.stdout and "big.py" in cp.stdout, cp.stdout


def test_gate_red_when_baseline_value_grows(tmp_path):
    (tmp_path / "god.gate.json").write_text(json.dumps({"max_file_lines": 100000}),
                                            encoding="utf-8")
    (tmp_path / "f.py").write_text("y = 1\n", encoding="utf-8")
    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0
    (tmp_path / "f.py").write_text("y = 1\n" * 200, encoding="utf-8")     # 1 行 → 200 行
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode != 0 and "不许变胖" in cp.stdout, cp.stdout


def test_mask_handles_lifetimes_and_byte_literals():
    """金丝雀：`&'static str` / `b'{'` 不许把掩码带跑。

    老版把 `'` 一律当字符字面量起点、一路吃到下一个 `'` ⇒ `&'static str` 吞掉中间整段（含
    花括号）⇒ 配平失真：真实 307 行的 scan_rust 被量成 **63 行**写进基线（假绿），而拆出的
    102 行 helper 又被量成 **235 行**（假红）。两侧都错，所以这条测试锁住"两侧都对"。
    """
    gg = _load_gate()
    src = ("fn f() {\n"
           "    let a = b'{';\n"
           "    let b = '{';\n"
           "    let c = \"}\";\n"
           "    let d: &'static str = \"x\";\n"
           "    let e = 'a';\n"
           "    let g = '\\n';\n"
           "    if a == b'[' && b == b'}' {\n"
           "        let h = 1;\n"
           "    }\n"
           "}\n")
    masked = gg._mask(src)
    assert (masked.count("{"), masked.count("}")) == (2, 2), masked
    assert [m for m in gg.brace_metrics(src) if m[2] == "fn"] == [("f", 11, "fn")], \
        gg.brace_metrics(src)
    # JS 家族：`'…'` 是字符串（不是字符字面量），含花括号也要掩掉
    js = "function f() {\n  var a = '}';\n  var b = '{';\n  return 1;\n}\n"
    assert [m for m in gg.brace_metrics(js, js=True) if m[2] == "fn"] == [("f", 5, "fn")], \
        gg.brace_metrics(js, js=True)


def test_file_growth_allowed_only_when_fn_shrinks(tmp_path):
    """金丝雀：**拆长函数必然让文件变长**——门要放行，且逐条打印（否则正确重构被判红）。"""
    cfg = {"max_file_lines": 100000, "max_fn_lines": 100000, "max_type_members": 100000,
           "file_growth_with_fn_shrink_pct": 10,
           "include": ["**/*.py"], "exclude": [], "baseline": "god-baseline.json"}
    (tmp_path / "god.gate.json").write_text(json.dumps(cfg), encoding="utf-8")
    f = tmp_path / "m.py"
    body = "\n".join(f"    v{i} = {i}" for i in range(38))
    f.write_text("def big():\n" + body + "\n", encoding="utf-8")
    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0

    half = "\n".join(f"    v{i} = {i}" for i in range(19))
    f.write_text("def part1():\n" + half + "\n\ndef part2():\n" + half + "\n", encoding="utf-8")
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode == 0, "拆函数被误判红（门会逼人关掉它）：\n" + cp.stdout
    assert "⇄" in cp.stdout, "放行必须逐条打印，不能静默：\n" + cp.stdout

    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0
    f.write_text(f.read_text(encoding="utf-8") + "".join(f"z{i} = {i}\n" for i in range(30)),
                 encoding="utf-8")                                        # 只变长、函数没变短
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode != 0, "函数没变短却涨行数 ⇒ 必须红：\n" + cp.stdout
    assert "不许变胖" in cp.stdout or "超出拆函数放行幅度" in cp.stdout, cp.stdout
