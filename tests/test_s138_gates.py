"""S138 契约：审计/自攻门脚本（CI 自攻门 + Mimosa 复审仪式两件）。

- attack_gate.py：dogfood attack_cruise，verdict 必须 clean（CI step 同款）；
- audit_copy.py：副本落仓库外、含文件数/HEAD 记账行；
- audit_diff.py：两份 report.md 标题集差量，新增非空即红（--allow-added 降级）。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools  # noqa: E402,F401


def _run(args, **kw):
    return subprocess.run([sys.executable, "-X", "utf8", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, cwd=ROOT, **kw)


def test_attack_gate_runs_clean():
    """CI step 同款：verdict 必须 clean（含四靶模糊+大输入+门自审+路径探针）。"""
    cp = _run([os.path.join(ROOT, "scripts", "attack_gate.py")])
    assert cp.returncode == 0, f"{cp.stdout[-400:]}\n{cp.stderr[-400:]}"
    assert "ATTACK-GATE OK" in cp.stdout


def test_audit_copy_refuses_inside_repo(tmp_path):
    cp = _run([os.path.join(ROOT, "scripts", "audit_copy.py"),
               os.path.join(ROOT, "tmp_audit_copy_should_refuse")])
    assert cp.returncode != 0
    assert "拒绝" in (cp.stdout + cp.stderr)


def test_audit_copy_and_diff_roundtrip(tmp_path):
    dest = tmp_path / "copy_out"
    # S145：本测试只验副本/差量机制——脏树护栏另有专测（test_s145_gates），
    # 这里显式 --allow-dirty，让本用例在"开发中（脏树）"与"CI（净树）"行为一致。
    cp = _run([os.path.join(ROOT, "scripts", "audit_copy.py"), str(dest),
               "--allow-dirty"])
    assert cp.returncode == 0, cp.stderr[-300:]
    m = re.search(r"AUDIT-COPY OK dest=(.+?) files=(\d+) head=(\S+)", cp.stdout)
    assert m and int(m.group(2)) > 100, cp.stdout      # 仓库级文件数
    assert os.path.isdir(dest)

    old_md = tmp_path / "old.md"
    new_md = tmp_path / "new.md"
    old_md.write_text("### HIGH · 路径穿越 (a.py:1)\n### HIGH · 命令注入 (b.py:2)\n",
                      encoding="utf-8")
    new_md.write_text("### HIGH · 路径穿越 (a.py:1)\n", encoding="utf-8")
    ok = _run([os.path.join(ROOT, "scripts", "audit_diff.py"),
               str(old_md), str(new_md)])
    assert ok.returncode == 0, ok.stdout
    assert "gone=1 added=0" in ok.stdout and "b.py:2" in ok.stdout

    # 新增非空 → 红（回归信号）；--allow-added 降级
    rev = _run([os.path.join(ROOT, "scripts", "audit_diff.py"),
                str(new_md), str(old_md)])
    assert rev.returncode != 0 and "ADDED" in rev.stdout
    okd = _run([os.path.join(ROOT, "scripts", "audit_diff.py"),
                str(new_md), str(old_md), "--allow-added"])
    assert okd.returncode == 0, okd.stdout
