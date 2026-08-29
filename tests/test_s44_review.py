# -*- coding: utf-8 -*-
"""S44 code_review：多透镜 + diff 模式回归。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

os.environ.setdefault("UNIFIED_RX_SANDBOX", r"D:\开发" + ";" + r"C:\Users\lbx13\AppData\Local\Temp\unified-rx-pytest")


def call_tool(name, args):
    r = registry.call(name, args)
    res = r.get("result", r)
    if "ok" not in res and "ok" in r:
        res = {"ok": r["ok"], **res}
    return res


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text(
        "def ok_fn(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    return str(tmp_path)


def test_security_lens_full_file(tmp_path):
    _repo(tmp_path)
    (tmp_path / "sec.py").write_text(
        'API_KEY = "sk-1234567890abcdef"\n'
        "import os\n"
        "os.system(user_cmd)\n", encoding="utf-8")
    r = call_tool("code_review", {"path": str(tmp_path / "sec.py")})
    lenses = [f["lens"] for f in r["findings"]]
    assert "security" in lenses
    sec = [f for f in r["findings"] if f["lens"] == "security"]
    assert any("硬编码凭据" in f["msg"] for f in sec)
    assert any("os.system" in f["msg"] for f in sec)


def test_diff_mode_only_changed_lines(tmp_path):
    repo = _repo(tmp_path)
    # 未提交改动：ok_fn 里引入 secret + eval（改动行）；旧文件其它行不动
    (tmp_path / "app.py").write_text(
        'def ok_fn(a, b):\n'
        '    token = "supersecret-123456"\n'
        '    eval(user_input)\n'
        '    return a + b\n', encoding="utf-8")
    r = call_tool("code_review", {"path": repo, "mode": "diff"})
    assert r["mode"] == "diff"
    files_hit = {f["file"] for f in r["findings"]}
    assert any(f.endswith("app.py") for f in files_hit)
    # 改动行内的 security 发现必须报
    sec = [f for f in r["findings"] if f["lens"] == "security"]
    assert any("硬编码凭据" in f["msg"] for f in sec)
    assert any("eval" in f["msg"] for f in sec)


def test_complexity_lens_long_function(tmp_path):
    _repo(tmp_path)
    body = "\n".join(f"    x{i} = {i}" for i in range(90))
    (tmp_path / "long.py").write_text(f"def long_fn():\n{body}\n", encoding="utf-8")
    r = call_tool("code_review", {"path": str(tmp_path / "long.py")})
    cx = [f for f in r["findings"] if f["lens"] == "complexity"]
    assert any("long_fn" in f["msg"] for f in cx)


def test_todo_lens(tmp_path):
    _repo(tmp_path)
    (tmp_path / "t.py").write_text("# FIXME: 以后修\ndef f():\n    pass\n",
                                   encoding="utf-8")
    r = call_tool("code_review", {"path": str(tmp_path / "t.py")})
    assert any(f["lens"] == "todo" for f in r["findings"])
