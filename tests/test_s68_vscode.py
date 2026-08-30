# -*- coding: utf-8 -*-
"""S68：VS Code 后手入口 + 多项目联动体检。

VS Code 用 stub exe 测试（bat 写标记文件）；multi_check 用两个 tmp git 仓
（一好一坏）验证汇总排序。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

AUTH = {"__authorized": True}


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)


def _stub_code(tmp_path):
    """假 Code.exe：bat 把参数写进标记文件（验证拉起与参数传递）。"""
    stub = tmp_path / "fake_code.bat"
    marker = tmp_path / "marker.txt"
    stub.write_text(f"@echo %* >> {marker}\n", encoding="utf-8")
    return str(stub), str(marker)


# ---------- ide_vscode ----------

def test_vscode_open_with_stub(tmp_path):
    stub, marker = _stub_code(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "m.rs"
    f.write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ide_vscode", {**AUTH, "action": "open",
                                     "paths": [str(f) + ":3:2", str(proj)],
                                     "exe": stub})
    assert r["ok"], r.get("error")
    assert r["result"]["opened"] == [str(f), str(proj)]
    # 分离式拉起是异步的：轮询等 stub 写完标记
    marker_seen = False
    import time
    for _ in range(30):
        if os.path.exists(marker):
            marker_seen = True
            break
        time.sleep(0.1)
    assert marker_seen, "stub 未被拉起"
    body = open(marker, encoding="utf-8").read()
    assert "-g" in body and "3:2" in body   # goto 定位透传


def test_vscode_diff_and_missing_exe(tmp_path):
    stub, marker = _stub_code(tmp_path)
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("x = 2\n", encoding="utf-8")
    r = registry.call("ide_vscode", {**AUTH, "action": "diff",
                                     "a": str(a), "b": str(b), "exe": stub})
    assert r["ok"] and r["result"]["action"] == "diff"
    r2 = registry.call("ide_vscode", {**AUTH, "action": "open",
                                      "paths": [str(a)],
                                      "exe": str(tmp_path / "ghost.exe")})
    assert not r2["ok"] and "不存在" in r2["error"]


def test_vscode_requires_auth(tmp_path):
    r = registry.call("ide_vscode", {"action": "open", "paths": [str(tmp_path)]})
    assert not r["ok"] and "授权" in r["error"]


# ---------- ide_multi_check ----------

def test_multi_check_sorts_bad_first(tmp_path):
    good = tmp_path / "good_proj"
    good.mkdir()
    (good / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(good)
    bad = tmp_path / "bad_proj"
    bad.mkdir()
    (bad / "evil.py").write_text(
        "def f(u):\n    return eval(u)\n", encoding="utf-8")
    _git_init(bad)
    r = registry.call("ide_multi_check", {**AUTH, "paths": [str(good), str(bad)]})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["total"] == 2 and res["bad"] == 1
    assert res["projects"][0]["path"] == str(bad)   # issues 排最前
    assert res["verdict"] == "issues"


def test_multi_check_clean_pair(tmp_path):
    good = tmp_path / "p1"
    good.mkdir()
    (good / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git_init(good)
    r = registry.call("ide_multi_check", {**AUTH, "paths": [str(good)]})
    res = r["result"]
    assert res["verdict"] in ("clean", "warn")
    assert res["bad"] == 0


def test_multi_check_requires_auth(tmp_path):
    r = registry.call("ide_multi_check", {"paths": [str(tmp_path)]})
    assert not r["ok"] and "授权" in r["error"]
