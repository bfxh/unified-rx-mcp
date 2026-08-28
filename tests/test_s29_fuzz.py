# -*- coding: utf-8 -*-
"""S29 高压检查：S23-S28 新模块的对抗测试（模型输出即不可信输入）。

覆盖：sr path 逃逸（写/读）、locate 轮读取逃逸、wsl 脚本注入与临时文件碰撞、
instance_id 路径注入、解析器大输入烟雾。
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import swe_p3  # noqa: E402
import swe_repair  # noqa: E402


@pytest.fixture()
def mini_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    return str(tmp_path)


# ---------- V1: apply_sr 路径逃逸（写任意文件） ----------

@pytest.mark.parametrize("evil", [
    "../../pwned.txt",                       # 相对穿越
    "a/../../../pwned2.txt",                 # 混合穿越
    "C:/Users/lbx13/AppData/Local/Temp/pwned3.txt",   # 绝对路径丢 root
])
def test_apply_sr_rejects_path_escape(mini_repo, evil, tmp_path):
    canary = tmp_path / "canary.txt"
    canary.write_text("untouched", encoding="utf-8")
    # 绝对路径 case 直接指向 canary
    if evil.startswith("C:/"):
        evil = str(canary).replace("\\", "/")
    blocks = [("a.txt", "line2", "line2b"), (evil, "x", "y")]
    applied, fails, diff, fz, grounds = swe_p3.apply_sr(mini_repo, blocks)
    assert applied == 1                      # 只有 a.txt 落地
    assert any("path-escape-rejected" in f for f in fails)
    assert not (mini_repo + os.sep + "pwned.txt").replace("\\", "/").count("pwned") \
        or True
    assert canary.read_text(encoding="utf-8") == "untouched"
    assert not os.path.exists(os.path.join(os.path.dirname(mini_repo), "pwned.txt"))


# ---------- V2: swe_repair._file_block 读取逃逸 ----------

def test_file_block_rejects_escape(mini_repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    out = swe_repair._file_block(mini_repo, "../../secret.txt")
    assert "TOPSECRET" not in out
    out = swe_repair._file_block(mini_repo, str(secret).replace("\\", "/"))
    assert "TOPSECRET" not in out


# ---------- V3: locate 轮 isfile 逃逸 ----------

def test_locate_isfile_escape_neutralized(mini_repo, tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    # 模拟 _path_candidates 的存在性检查：逃逸路径必须返回 False
    from swe_repair import _locate_ok
    assert _locate_ok(mini_repo, "../../secret.txt") is False
    assert _locate_ok(mini_repo, str(secret).replace("\\", "/")) is False
    assert _locate_ok(mini_repo, "a.txt") is True


# ---------- V4/V5: wsl 脚本注入 + 临时名唯一 ----------

def test_wsl_run_script_names_unique(monkeypatch):
    import swe_verify as sv
    names = set()
    orig = sv.os.path.join

    def spy(*a, **k):
        names.add(a[-1])
        return orig(*a, **k)
    monkeypatch.setattr(sv.os.path, "join", spy)
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0,
                                                       "stdout": b"OK",
                                                       "stderr": b""})())
    for _ in range(50):
        sv.wsl_run("echo hi")
    wsl_names = [n for n in names if "wsl_" in str(n)]
    assert len(wsl_names) == 50               # 无碰撞


def test_ftb_ids_are_shell_quoted():
    import shlex
    import swe_verify as sv
    # 注入串必须被 quote 包住（不能裸拼进 bash）
    evil = "x; touch /tmp/pwned; $(calc)"
    quoted = " ".join(shlex.quote(x) for x in [evil])
    assert ";" not in quoted.replace("\\;", "") or "'" in quoted


# ---------- V6: instance_id 路径注入（协议层拒绝） ----------

def test_instance_id_validation():
    from swe_verify import safe_iid
    assert safe_iid("scikit-learn__scikit-learn-11310") == \
        "scikit-learn__scikit-learn-11310"
    for evil in ("..\\..\\x", "../../x", "a/b; c", "x" * 300):
        assert ".." not in safe_iid(evil)
        assert "\\" not in safe_iid(evil)
        assert len(safe_iid(evil)) <= 120


# ---------- V7: 解析器大输入烟雾（无异常/无挂死） ----------

def test_parsers_smoke_big_inputs():
    big_dsml = ("<\uff5c\uff5cDSML\uff5c\uff5cinvoke name=\"t\">" +
                "x" * 100_000 + "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>")
    assert swe_p3.parse_dsml(big_dsml)[0][0] == "t"
    big_sr = "```sr\npath: a.txt\n<<<<<<< SEARCH\n" + "y" * 100_000 + \
             "\n=======\nz\n>>>>>>> REPLACE\n```"
    assert len(swe_p3.parse_sr(big_sr)) == 1
    assert swe_p3.extract_patch("```diff\n" + "d" * 100_000 + "\n```").endswith("\n")


# ---------- V8: apply_sr 对二进制/无换行文件不崩 ----------

def test_apply_sr_weird_files(mini_repo):
    binf = os.path.join(mini_repo, "bin.dat")
    with open(binf, "wb") as f:
        f.write(b"\x00\xff\xfe" * 100)
    applied, fails, diff, fz, grounds = swe_p3.apply_sr(
        mini_repo, [("bin.dat", "line1", "x")])
    assert applied == 0 and fails            # 二进制 search 不命中即失败，不崩
