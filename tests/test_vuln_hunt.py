"""S171：一键漏洞挖掘（`scripts/vuln_hunt.py`）的三合一与隔离纪律——金丝雀。

判据都是"真会红"的那种：
 ① **植入的三条最小可检面必须被抓到**：形参→命令汇点的污点边 / 明文凭据 / JS 危险面；
 ② **原件只读**：跑前后对原件做内容级指纹（路径+sha256），必须一字不差
    （审计只发生在克隆体上——这是本仓的隔离红线）；
 ③ 克隆体落在沙箱内 + 默认清理 / `--keep` 保留；
 ④ 封印可复核：同输入两次跑 ⇒ 封印一致（覆盖目标名/三面结果/发现，不含路径与时间）。

样本用**碎片拼接**生成（同仓内夹具纪律：危险字面量靠拼接才成形，否则静态扫描会把夹具
本身判成真漏洞）。Rust exe 未构建时如实 skip（app_clone/taint 都走 exe）。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools.appaudit import _rs_exe  # noqa: E402

_CMD = "os." + "sys" + "tem"
_EVAL = "ev" + "al"
_EXEC = "cp.ex" + "ec"
_NEED_EXES = ("rx-appops.exe", "rx-svc.exe")


def _mk_sample(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.py").write_text(
        "import os\n\n\ndef handler(user_input):\n"
        f"    {_CMD}('echo ' + user_input)\n", encoding="utf-8")
    (d / "main.js").write_text(
        "const cp = require('child_process');\n"
        f"function boot(x) {{ return {_EVAL}(x); }}\n"
        f"{_EXEC}('ls ' + process.argv[2]);\n", encoding="utf-8")
    (d / "secrets.env").write_text(
        "AWS=AKIAXW2345678901QRST\nGITHUB=ghp_" + "A" * 34 + "\n", encoding="utf-8")
    return d


def _fingerprint(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        h.update(p.relative_to(d).as_posix().encode())
        if p.is_file():
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _run(target: Path, tmp: Path, *extra: str) -> tuple[subprocess.CompletedProcess, Path]:
    report = tmp / "report.json"
    cp = subprocess.run([sys.executable, "-X", "utf8", "scripts/vuln_hunt.py",
                         "--target", str(target), "--sandbox", str(tmp / "hunt"),
                         "--report", str(report), "--json", *extra],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=ROOT, shell=False, timeout=900)
    return cp, report


@pytest.mark.skipif(not all(_rs_exe(n) for n in _NEED_EXES),
                    reason="Rust exe 未构建（app_clone/taint 都走 exe）")
def test_vuln_hunt_catches_planted_bugs_and_keeps_original_readonly(tmp_path):
    src = _mk_sample(tmp_path / "sample")
    before = _fingerprint(src)
    cp, report = _run(src, tmp_path)
    assert cp.returncode == 0, cp.stdout[-600:] + cp.stderr[-800:]
    rep = json.loads(report.read_text(encoding="utf-8"))

    assert rep["verdict"] == "issues", rep
    assert rep["failed_faces"] == [], f"有面没跑成: {rep['failed_faces']}"
    blob = json.dumps(rep, ensure_ascii=False)
    assert "os.system" in blob and "user_input" in blob, "污点面没抓到形参→命令汇点"
    assert "aws_access_key" in blob, "秘密面没抓到明文凭据"
    assert rep["seal"].startswith("sha256:") and len(rep["seal"]) == 71, rep["seal"]

    # ② 原件只读（内容级）
    assert _fingerprint(src) == before, "原件被改写了——审计必须在克隆体上"
    # ③ 克隆体在沙箱内 + 默认清理
    snap = Path(rep["snapshot"])
    assert snap.is_relative_to(Path(rep["sandbox"])), f"克隆体不在沙箱内: {snap}"
    assert not snap.exists(), "默认应当清理克隆体（要留用 --keep）"


@pytest.mark.skipif(not all(_rs_exe(n) for n in _NEED_EXES),
                    reason="Rust exe 未构建")
def test_vuln_hunt_keep_and_seal_is_reproducible(tmp_path):
    """`--keep` 保留克隆体；**同输入 ⇒ 同封印**（封印不含路径/时间，才谈得上可复核）。"""
    src = _mk_sample(tmp_path / "sample")
    cp1, r1 = _run(src, tmp_path / "run1")
    assert cp1.returncode == 0, cp1.stdout[-600:] + cp1.stderr[-800:]
    rep1 = json.loads(r1.read_text(encoding="utf-8"))
    assert not Path(rep1["snapshot"]).exists(), "默认应清理"

    cp2, r2 = _run(src, tmp_path / "run2", "--keep")
    assert cp2.returncode == 0, cp2.stdout[-600:] + cp2.stderr[-800:]
    rep2 = json.loads(r2.read_text(encoding="utf-8"))
    assert Path(rep2["snapshot"]).is_dir(), "--keep 应保留克隆体"
    assert rep1["seal"] == rep2["seal"], "同输入两次跑封印不一致 ⇒ 封印含了挥发字段"
