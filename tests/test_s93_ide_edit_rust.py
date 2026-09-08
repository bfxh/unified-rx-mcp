# -*- coding: utf-8 -*-
"""S93 ide_edit 定位三件 Rust 化契约测试：薄壳（tools/ide_edit.py）→ rx-ide.exe。

S93 起旧 Python 实现的职责移入 Rust（tests at rust/tests/ide_test.rs 打同一
语义）；本文件守住 Python 侧注册面契约：
- 沙盒拒绝 / fail-closed = ValueError 包络（registry ok:false），
  工具级错误 = result.error 字段（locate/rename 的解析类失败走工具级 road）；
- 语义怪癖原样保留：忽略大小写命中、limit*3 双层停机、refs 含触发停机的
  文件、RAW split 保留 \r、负 cursor 的 end 负值 + Python 负切片、
  10MB getsize 门在沙盒之前（沙盒外文件报"文件不可读"）、
  空符号 count("")=len+1、固定 200 上限、junction 下钻 / 悬空静默剪；
- 注册 schema 与 ide.py 门面 re-export 不变；
- exe 缺失走清晰报错，不静默降级。
"""
import os
import subprocess

import pytest

import registry
import tools  # noqa: F401
from tools import ide as ide_facade  # noqa: F401  门面 re-export 契约
from tools import ide_edit


@pytest.fixture()
def open_sandbox(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")


@pytest.fixture()
def codebase(tmp_path):
    # 字节写控制换行（RAW split 保 \r 的口径按磁盘字节算）
    (tmp_path / "aa.py").write_bytes(
        b"hello_world = 1\nHELLO_WORLD twice\nfind hello_world here\n"
        b"hello_world again\nlast line\n")
    (tmp_path / "ac.py").write_text("x = hello_world + HELLO_WORLD\n",
                                    encoding="utf-8")
    (tmp_path / "ad.txt").write_text("hello_world in txt\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "ba.py").write_text("hello_world\n", encoding="utf-8")
    (tmp_path / ".git" / "camouflaged.py").parent.mkdir(exist_ok=True)
    (tmp_path / ".git" / "camouflaged.py").write_text("hello_world hidden\n",
                                                      encoding="utf-8")
    return tmp_path


# ---------- locate_edit ----------

def test_locate_hits_refs_and_ci(codebase, open_sandbox):
    r = registry.call("locate_edit", {"path": str(codebase), "query": "hello_world"})
    assert r["ok"], r
    res = r["result"]
    # aa.py 4 处命中（含 HELLO_WORLD 的忽略大小写路）+ ac.py 1 + sub/ba.py 1
    assert res["total"] == 6
    # 影响面计数区分大小写：HELLO_WORLD 两处不入 refs
    assert res["references_in_scan"] == 5
    assert res["query"] == "hello_world"
    hits = res["hits"]
    assert hits[0]["line"] == 1
    # snippet 窗口：1 前导 + 当前行 + 2 后随
    assert hits[0]["snippet"] == ("hello_world = 1\nHELLO_WORLD twice\n"
                                  "find hello_world here\nhello_world again")
    # 非代码文件不进 hits 也不进 refs
    assert all(not h["file"].endswith("ad.txt") for h in hits)


def test_locate_limit_stop(codebase, tmp_path, open_sandbox):
    for i in range(1, 9):
        (tmp_path / f"f{i}.py").write_text("zzq\nzzq\nzzq\n", encoding="utf-8")
    r = registry.call("locate_edit", {"path": str(tmp_path), "query": "zzq",
                                      "limit": 4})
    assert r["ok"], r
    res = r["result"]
    assert res["total"] == 12          # limit*3=12 停在第 4 个文件
    assert res["references_in_scan"] == 12  # refs 含触发停机的文件
    assert len(res["hits"]) == 4       # hits[:limit]


def test_locate_max_files_and_skip_dirs(codebase, open_sandbox):
    r = registry.call("locate_edit", {"path": str(codebase), "query": "hello_world",
                                      "max_files": 2})
    assert r["ok"], r
    # max_files 只计代码文件：aa.py + ac.py（ad.txt 不占额度）
    assert r["result"]["total"] == 5
    files = {h["file"] for h in r["result"]["hits"]}
    assert all(f.endswith(("aa.py", "ac.py")) for f in files)
    # .git 跳过目录不进
    assert all(".git" not in f for f in files)


def test_locate_empty_query_and_errors(codebase, open_sandbox):
    r = registry.call("locate_edit", {"path": str(codebase), "query": "   "})
    assert not r["ok"] and "query 为空" in r["result"]["error"]
    r2 = registry.call("locate_edit", {"path": str(codebase / "aa.py"),
                                       "query": "x"})
    assert not r2["ok"] and "不是目录" in r2["result"]["error"]
    r3 = registry.call("locate_edit", {"path": "", "query": "x"})
    assert not r3["ok"] and r3["result"]["error"] == "path 必填"


def test_locate_unicode_query(tmp_path, open_sandbox):
    (tmp_path / "af.py").write_text("你好 = 1\n调用 你好\n", encoding="utf-8")
    r = registry.call("locate_edit", {"path": str(tmp_path), "query": "你好"})
    assert r["ok"], r
    assert r["result"]["total"] == 2 and r["result"]["references_in_scan"] == 2


# ---------- code_context ----------

def test_context_window_and_radius(tmp_path, open_sandbox):
    f = tmp_path / "long20.py"
    f.write_text("\n".join(f"l{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    r = registry.call("code_context", {"path": str(f), "cursor_line": 10,
                                       "radius": 3})
    assert r["ok"], r
    res = r["result"]
    assert (res["start"], res["end"]) == (5, 14)   # radius 下限 5
    assert res["total_lines"] == 21                # 尾幻影行
    assert res["lang"] == "python"
    assert res["file"] == str(f)                   # 原样参数，不重写
    # radius=0 视作缺省 30；radius=500 钳到 200
    r0 = registry.call("code_context", {"path": str(f), "cursor_line": 10,
                                        "radius": 0})
    assert (r0["result"]["start"], r0["result"]["end"]) == (1, 21)
    # cursor=0 → 头窗
    rh = registry.call("code_context", {"path": str(f)})
    assert rh["result"]["start"] == 1 and rh["result"]["end"] == 21


def test_context_raw_split_and_negative_cursor(tmp_path, open_sandbox):
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"l1\r\nl2\r\nl3\r\nl5\r\n")
    r = registry.call("code_context", {"path": str(crlf), "cursor_line": 2,
                                       "radius": 5})
    assert r["result"]["total_lines"] == 5
    assert r["result"]["content"] == "l1\r\nl2\r\nl3\r\nl5\r\n"  # \r 原样保留
    # 负 cursor：end 负值 + Python 负切片（end=-6 → 13-6=7 行）
    tw = tmp_path / "tail12.py"
    tw.write_bytes(b"".join(b"t%d\n" % i for i in range(1, 13)))
    rn = registry.call("code_context", {"path": str(tw), "cursor_line": -10,
                                        "radius": 5})
    assert rn["result"]["end"] == -6
    assert rn["result"]["content"] == "t1\nt2\nt3\nt4\nt5\nt6\nt7"


def test_context_gates(tmp_path, open_sandbox):
    # >10MB 拒读
    big = tmp_path / "over10mb.py"
    big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    r = registry.call("code_context", {"path": str(big)})
    assert not r["ok"] and r["result"]["error"] == "文件超过 10MB——拒绝读取"
    # 恰好 10MB：不拒（> 才门）
    bound = tmp_path / "exact10mb.py"
    bound.write_bytes(b"x" * (10 * 1024 * 1024))
    rb = registry.call("code_context", {"path": str(bound)})
    assert rb["ok"] and rb["result"]["total_lines"] == 1
    # 缺失 / 目录 / 空路径 → 文件不可读
    miss = registry.call("code_context", {"path": str(tmp_path / "no_such.py")})
    assert not miss["ok"] and "文件不可读" in miss["result"]["error"]
    d = registry.call("code_context", {"path": str(tmp_path)})
    assert not d["ok"] and "文件不可读" in d["result"]["error"]
    e = registry.call("code_context", {"path": ""})
    assert not e["ok"] and e["result"]["error"] == "文件不可读: "


def test_context_lang_table(tmp_path, open_sandbox):
    # 10 扩展名判型表（与 scan 的 21 表不同源，缺省 text）
    for name, lang in (("a.gd", "gdscript"), ("b.cs", "csharp"),
                       ("c.dart", "dart"), ("d.tsx", "typescript"),
                       ("e.jsx", "javascript"), ("f.rs", "rust"),
                       ("g.go", "go"), ("h.md", "text")):
        p = tmp_path / name
        p.write_text("x = 1\n" if lang != "text" else "x = 1\n",
                     encoding="utf-8")
        r = registry.call("code_context", {"path": str(p)})
        assert r["result"]["lang"] == lang, name


# ---------- ide_rename ----------

def test_rename_plan_and_quirks(tmp_path, open_sandbox):
    # 字节写：空符号 count("")=len+1 按磁盘字节数（CRLF 会多算 \r）
    (tmp_path / "one.py").write_bytes(b"foo = 1\nfoo(2)\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "two.py").write_bytes(b"bar\nfoo\n")
    (tmp_path / "three.txt").write_bytes(b"foo\n")
    r = registry.call("ide_rename", {"root": str(tmp_path), "symbol": "foo",
                                     "new_name": "bar"})
    assert r["ok"], r
    res = r["result"]
    assert res["files_affected"] == 2 and res["total_occurrences"] == 3
    assert res["plan"] is None
    assert res["note"] == "L3 只建议不落盘；确认后可用 fs_write 应用"
    r2 = registry.call("ide_rename", {"root": str(tmp_path), "symbol": "foo",
                                      "new_name": "bar", "include_plan": True})
    plan = r2["result"]["plan"]
    assert len(plan) == 2
    assert plan[0]["file"].endswith("one.py") and plan[0]["occurrences"] == 2
    assert plan[1]["file"].endswith("two.py") and plan[1]["occurrences"] == 1
    # 大小写敏感
    r3 = registry.call("ide_rename", {"root": str(tmp_path), "symbol": "FOO",
                                      "new_name": "x"})
    assert r3["result"]["files_affected"] == 0
    # 空符号怪癖：count("")=len+1（15 字符 → 16；8 字符 → 9）
    r4 = registry.call("ide_rename", {"root": str(tmp_path), "symbol": "",
                                      "new_name": "x"})
    assert r4["result"]["files_affected"] == 2
    assert r4["result"]["total_occurrences"] == 25
    # path 必填 / 非目录
    r5 = registry.call("ide_rename", {"root": "", "symbol": "x", "new_name": "y"})
    assert not r5["ok"] and r5["result"]["error"] == "path 必填"
    r6 = registry.call("ide_rename", {"root": str(tmp_path / "one.py"),
                                      "symbol": "x", "new_name": "y"})
    assert not r6["ok"] and "不是目录" in r6["result"]["error"]


def test_rename_cap_200(tmp_path, open_sandbox):
    for i in range(1, 206):
        (tmp_path / f"c{i:03d}.py").write_text("sym\n", encoding="utf-8")
    r = registry.call("ide_rename", {"root": str(tmp_path), "symbol": "sym",
                                     "new_name": "n", "include_plan": True})
    assert r["ok"], r
    res = r["result"]
    assert res["files_affected"] == 200 and res["total_occurrences"] == 200
    assert len(res["plan"]) == 200
    assert res["plan"][0]["file"].endswith("c001.py")
    assert res["plan"][199]["file"].endswith("c200.py")


# ---------- 遍历 junction 语义（S86 口径，os.walk 3.14）----------

@pytest.mark.skipif(os.name != "nt", reason="junction 仅 Windows")
def test_walk_junctions(tmp_path, open_sandbox):
    target = tmp_path / "jtarget"
    target.mkdir()
    (target / "jt.py").write_text("junc_hit\n", encoding="utf-8")
    root = tmp_path / "jroot"
    root.mkdir()
    for name, dest in (("jlink", target), ("dangle", tmp_path / "no_such_dir")):
        cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / name),
                             str(dest)], capture_output=True)
        assert cp.returncode == 0, cp.stderr
    r = registry.call("locate_edit", {"path": str(root), "query": "junc_hit"})
    assert r["ok"], r
    # junction 照常下钻（os.walk islink()=False），悬空联接静默剪
    assert r["result"]["total"] == 1
    assert r["result"]["hits"][0]["file"].endswith("jlink\\jt.py")


# ---------- 沙盒包络 ----------

def test_sandbox_deny_tool_error_road(codebase, monkeypatch):
    # locate 的解析类失败按旧实现走工具级 {"error": ...}（exit 0 包络）
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "Z:\\no-such-root-xyz")
    r = registry.call("locate_edit", {"path": str(codebase), "query": "x"})
    assert not r["ok"] and "路径越界" in r["error"]
    # code_context：getsize 成功后 _read 才失败 → "文件不可读"（旧漏斗保留）
    r2 = registry.call("code_context", {"path": str(codebase / "aa.py")})
    assert not r2["ok"] and "文件不可读" in r2["result"]["error"]


def test_fail_closed_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_RX_SANDBOX", raising=False)
    r = registry.call("locate_edit", {"path": str(tmp_path), "query": "x"})
    assert not r["ok"]


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    bogus = tmp_path / "not-an-exe.exe"
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", str(bogus))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", "*")
    r = registry.call("code_context", {"path": str(tmp_path)})
    assert not r["ok"] and "rx-ide.exe 不存在" in r["error"]


# ---------- 注册面契约 ----------

def test_schemas_unchanged():
    assert registry._TOOLS["locate_edit"]["schema"]["required"] == ["path", "query"]
    assert registry._TOOLS["locate_edit"]["schema"]["properties"]["max_files"]\
        ["type"] == "integer"
    assert registry._TOOLS["locate_edit"]["schema"]["properties"]["limit"]\
        ["type"] == "integer"
    assert registry._TOOLS["code_context"]["schema"]["required"] == ["path"]
    assert registry._TOOLS["code_context"]["schema"]["properties"]["cursor_line"]\
        ["type"] == "integer"
    assert registry._TOOLS["code_context"]["schema"]["properties"]["radius"]\
        ["type"] == "integer"
    assert registry._TOOLS["ide_rename"]["schema"]["required"] == \
        ["root", "symbol", "new_name"]
    assert registry._TOOLS["ide_rename"]["schema"]["properties"]["include_plan"]\
        ["type"] == "boolean"
    for t in ("locate_edit", "code_context", "ide_rename"):
        assert registry._TOOLS[t]["group"] == "ide"


def test_thin_shell_retirement():
    # S93：遍历/命中/窗口/计数全部退役到 rust/src/ide.rs
    src = open(os.path.join(os.path.dirname(tools.__file__), "ide_edit.py"),
               encoding="utf-8").read()
    assert '_rx_ide_call(["locate"' in src
    assert '_rx_ide_call(["context"' in src
    assert '_rx_ide_call(["rename"' in src
    # 旧实现机器退役（docstring 可提其名，代码必须走 exe）
    assert "all_sources" not in src
    assert "query.strip()" not in src
    assert "int(radius or 30)" not in src
    assert "_iter_files(root, 200)" not in src
    assert "os.path.getsize(path)" not in src
    # 门面 re-export 不变
    assert ide_facade.locate_edit is ide_edit.locate_edit
    assert ide_facade.code_context is ide_edit.code_context
    assert ide_facade.ide_rename is ide_edit.ide_rename
