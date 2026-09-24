"""S78 验收：S73 深扫重放（spec/VULN-HUNTING.md P1-a 的污点引擎验收题）。

重放对象：修复前快照 git 395e4cd（S73 修复提交 846280b 的父提交），
Mimosa scan-2026-09-04T15-42-49（seal sha256:32bfc234）58 条发现中人工核实
的 3 条真问题坐标：
  - tools/learn.py:38     lesson 显式 lessons_dir 任意路径写（形参直连 open）
  - tools/metrics.py:83   code_coverage script/source_dir 不过沙盒（join 传播到 open）
  - tools/appaudit.py:167 app_clone 整目录读（os.walk 目标传播到 open）

验收线（两条都必须过）：
  1) 3 条真问题在污点模式下一条不漏（文件+行号精确命中）；
  2) 同一快照上 污点命中数 ≤ ½ 朴素基线命中数（--naive = 模式匹配对照，
     任何含变量实参的汇点调用都报——即 S73 误报大头的形态）。
"""
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools.attack import _rx_taint_exe  # noqa: E402

SNAPSHOT = "395e4cd"
REALS = [
    ("tools/learn.py", 38),
    ("tools/metrics.py", 83),
    ("tools/appaudit.py", 167),
]


def _run_rx_taint(root, naive, extra=()):
    exe = _rx_taint_exe()
    assert exe, "rx-taint.exe 不存在——先 cargo build"
    env = dict(os.environ)
    env["UNIFIED_RX_SANDBOX"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [exe, root] + (["--naive"] if naive else []) + list(extra)
    cp = subprocess.run(argv, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=600, env=env)
    assert cp.returncode == 0, cp.stderr[-500:]
    return json.loads(cp.stdout)


@pytest.fixture(scope="module")
def snapshot_dir(tmp_path_factory):
    """git archive 出 395e4cd 快照到临时目录（只读导出，不动仓库）。"""
    cp = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", SNAPSHOT],
                        capture_output=True, timeout=120)
    assert cp.returncode == 0, f"git archive {SNAPSHOT} 失败: {cp.stderr[-300:]}"
    d = tmp_path_factory.mktemp(f"s73-replay-{SNAPSHOT}")
    dotdot = ".."
    with tarfile.open(fileobj=io.BytesIO(cp.stdout)) as tf:
        # 成员白名单：只放行不以盘符根开头、且不含父目录穿越片段的常规成员
        # （archive 来源是本仓固定提交，防御性过滤成本为零）
        safe = [m for m in tf.getmembers()
                if not m.name.startswith(("/", "\\"))
                and dotdot not in m.name.replace("\\", "/").split("/")]
        tf.extractall(d, members=safe)
    return d


def test_s73_replay(snapshot_dir):
    taint = _run_rx_taint(str(snapshot_dir), naive=False)
    naive = _run_rx_taint(str(snapshot_dir), naive=True)

    # 真问题必须以 definite（入口可达实锤）级命中
    t_def = {(f["file"], f["line"]) for f in taint["findings"]
             if f["kind"] == "definite"}
    missing = [(fp, ln) for fp, ln in REALS if (fp, ln) not in t_def]
    assert not missing, (
        "真问题未以实锤级命中: %r; definite 命中示例: %r" % (
            missing, sorted(t_def)[:20]))

    # 实锤级命中 ≤ ½ 朴素基线（朴素 = 模式匹配，任何变量实参汇点都报）
    nt = len(t_def)
    nn = len(naive["findings"])
    assert nt * 2 <= nn, (
        "污点版未压掉一半误报面: definite=%d naive=%d" % (nt, nn))

    # 记账行：验收数字进 ROUNDLOG 用
    t_all = len(taint["findings"])
    print("\nREPLAY S73 snapshot=%s files=%d taint_definite=%d taint_all=%d "
          "naive=%d reals=%d/%d"
          % (SNAPSHOT, taint["files_scanned"], nt, t_all, nn,
             len(REALS) - len(missing), len(REALS)))


def test_s128_cross_file_is_strict_superset(snapshot_dir):
    """S128 不变量：跨文件传播只加不减。

    - 发现集（file,line,sink,var）必须为 --no-cross 的超集（不吞旧发现）；
    - definite（实锤）数不得低于 --no-cross（跨文件只应升级或持平）；
    - 打印 A/B 增量供 ROUNDLOG 记账（新增的 cross 流条数）。
    """
    cross = _run_rx_taint(str(snapshot_dir), naive=False)
    base = _run_rx_taint(str(snapshot_dir), naive=False, extra=["--no-cross"])

    def key(f):
        return (f["file"], f["line"], f["sink"], f["var"])

    kc = {key(f) for f in cross["findings"]}
    kb = {key(f) for f in base["findings"]}
    assert kb <= kc, "跨文件不得吞发现；缺失: %r" % sorted(kb - kc)[:10]

    def nd(res):
        return sum(1 for f in res["findings"] if f["kind"] == "definite")

    n_cross = cross.get("cross_file_findings", 0)
    origin_ok = all("origin" in f for f in cross["findings"] if f["flow"] == "cross")
    assert origin_ok, "flow=cross 的发现必须带 origin 链证据"
    assert nd(cross) >= nd(base), (nd(cross), nd(base))
    print("\nS128 A/B snapshot=%s all=%d(+%d) definite=%d(+%d) cross_flows=%d "
          "ambiguous_skipped=%d"
          % (SNAPSHOT, len(kc), len(kc) - len(kb), nd(cross), nd(cross) - nd(base),
             n_cross, cross.get("cross_skipped_ambiguous", 0)))
