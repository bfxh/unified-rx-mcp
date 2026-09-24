"""S169：上帝对象门的**放行策略**守门测试（拆函数带来的合法交换）。

为什么要单独一个文件：这些金丝雀本身是"给门加通道"的测试，写在 `test_s167_god_gate.py` 里会把
那个文件顶过**文件行数棘轮**（棘轮只准减，测试增长也算胖）。拆出来后：老文件回到原尺寸（可收紧），
新文件按新基线判（只看阈值）。判据本身没放松。

锁三件事：
  1. 拆长函数 ⇒ 文件变长：`max_fn_lines` 严格下降且涨幅 ≤10% ⇒ **放行**，且逐条 `⇄` 打印（不静默）；
  2. 函数没变短 ⇒ 涨一行都红；
  3. 顺带引入的小结构体（≤阈 24 成员）⇒ 放行；**超阈类型连"函数变短"也不放行**。
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
    spec = importlib.util.spec_from_file_location("god_gate_under_test2", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(**over):
    cfg = {"max_file_lines": 100000, "max_fn_lines": 100000, "max_type_members": 100000,
           "file_growth_with_fn_shrink_pct": 10,
           "include": ["**/*.py"], "exclude": [], "baseline": "god-baseline.json"}
    cfg.update(over)
    return cfg


def test_file_growth_allowed_only_when_fn_shrinks(tmp_path):
    """金丝雀：**拆长函数必然让文件变长**——门要放行，且逐条打印（否则正确重构被判红）。"""
    (tmp_path / "god.gate.json").write_text(json.dumps(_cfg()), encoding="utf-8")
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
    assert "不许变胖" in cp.stdout or "函数只降" in cp.stdout, cp.stdout


def test_growth_allowed_when_fn_shrinks_more_than_file_grows(tmp_path):
    """金丝雀：**净账**通道——纯搬移时 helper 壳/说明注释会把文件顶过 pct 线；只要"最长函数减少
    的行数 ≥ 文件增加的行数"，净复杂度仍降 ⇒ 放行（否则正确的重构被比例线拦住）。"""
    (tmp_path / "god.gate.json").write_text(json.dumps(_cfg()), encoding="utf-8")
    f = tmp_path / "m.py"
    f.write_text("def big():\n" + "".join(f"    v{i} = {i}\n" for i in range(60)),
                 encoding="utf-8")
    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0
    # 20 行说明 + 拆成 30+30：文件 +20 行（>10%，pct 通道不给），但最长函数降 30 行 ⇒ 净账为负
    half = "".join(f"    v{i} = {i}\n" for i in range(30))
    doc = "".join(f"# 说明行 {i}（纯增行，模拟搬移时要补的注释/壳）\n" for i in range(20))
    f.write_text(doc + "def p1():\n" + half + "def p2():\n" + half, encoding="utf-8")
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode == 0, "净账为负的重构被误判红：\n" + cp.stdout
    assert "净账为负" in cp.stdout, cp.stdout


def test_small_type_allowed_only_with_fn_shrink(tmp_path):
    """金丝雀：拆函数时常顺手引入一个**小结构体**（≤阈）——放行；但超阈类型不许被放行。"""
    (tmp_path / "god.gate.json").write_text(
        json.dumps(_cfg(max_type_members=24, include=["**/*.rs"])), encoding="utf-8")
    f = tmp_path / "m.rs"
    body = "\n".join(f"    let v{i} = {i};" for i in range(48))
    f.write_text(f"fn big() {{\n{body}\n}}\n", encoding="utf-8")
    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0

    small = "\n".join(f"    let v{i} = {i};" for i in range(19))
    ty = ("struct Acc {\n" + "".join(f"    f{i}: usize,\n" for i in range(6)) + "}\n\n"
          "fn p1() {\n" + small + "\n}\n\nfn p2() {\n" + small + "\n}\n")
    f.write_text(ty, encoding="utf-8")
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode == 0, "小结构体 + 函数变短被误判红：\n" + cp.stdout
    assert "⇄" in cp.stdout, "放行必须逐条打印：\n" + cp.stdout

    # 30 个成员 > 阈值 24 ⇒ 硬阈值仍拦（即便函数变短、文件没胖多少）
    assert _run("--root", str(tmp_path), "--write-baseline").returncode == 0
    big_ty = ("struct Acc {\n" + "".join(f"    f{i}: usize,\n" for i in range(30)) + "}\n\n"
              "fn p1() {\n" + small + "\n}\n\nfn p2() {\n" + small + "\n}\n")
    f.write_text(big_ty, encoding="utf-8")
    cp = _run("--root", str(tmp_path), "--top", "0")
    assert cp.returncode != 0 and "max_type_members" in cp.stdout, \
        "超阈类型不许被放行：\n" + cp.stdout
