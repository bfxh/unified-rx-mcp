# -*- coding: utf-8 -*-
"""tests/test_s95_fs_back_contract.py —— S95 fs 读面回迁 golden master（oracle 收口）。

oracle 链路：回迁前 bench/s95_fs_golden.py 以现行 exe 薄壳捕获 40 场景 →
tests/fixtures/s95_fs_golden.json；本测试重放同一场景矩阵（场景表/语料/脱敏
直接 import 自捕获脚本，单一事实源），断言逐字段全等。回迁前先对 exe 跑绿 =
装置自检；tools/fs.py 回迁纯 Python 后再跑绿 = exe↔纯 Python 等价实证，
此后作为永久回归测试。

Windows-only（golden 为宿主平台捕获）；Linux 面由 tests/test_s95_linux_smoke.py 覆盖。
沙盒穿越不入矩阵：新旧两侧共享 tools.fs._resolve（按构造同语义），越界拒绝由
test_security_fuzz.py 全权锁定。
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "bench") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "bench"))

import s95_fs_golden as G  # noqa: E402
import registry  # noqa: E402
import tools  # noqa: E402,F401  注册工具面

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="golden 为 Windows 捕获；Linux 面走 tests/test_s95_linux_smoke.py")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("s95_corpus"))
    G.build_corpus(root)
    return root


def _live_rows(corpus, monkeypatch):
    monkeypatch.chdir(_ROOT)  # read_relative_missing 以仓库根为 cwd，与捕获一致
    rows = []
    for label, toolname, args_fn, sbx in G.SCENARIOS:
        s = sbx(corpus) if callable(sbx) else sbx
        if s is None:
            monkeypatch.delenv("UNIFIED_RX_SANDBOX", raising=False)
        else:
            monkeypatch.setenv("UNIFIED_RX_SANDBOX", s)
        out = registry.call(toolname, args_fn(corpus))
        rows.append({"label": label, "out": G.mask(out, corpus)})
    return rows


def test_golden_fixture_in_place():
    assert os.path.isfile(G.FIXTURE), "先跑 bench/s95_fs_golden.py 捕获 exe 臂"


def test_scenario_table_matches_golden():
    rows = json.loads(open(G.FIXTURE, encoding="utf-8").read())
    assert [r["label"] for r in rows] == [s[0] for s in G.SCENARIOS], (
        "场景表与 golden fixture 失步——先更新 bench/s95_fs_golden.py 再重新捕获")


def _semantic(out):
    """剔除 error_detail（S72 调试堆栈尾）：实现细节非契约——exe 臂经 _rx_fs_call
    抛出、纯 Python 臂在 _resolve 抛出，堆栈必然不同；error 文本本身逐字比对。"""
    if isinstance(out, dict):
        return {k: _semantic(v) for k, v in out.items() if k != "error_detail"}
    if isinstance(out, list):
        return [_semantic(x) for x in out]
    return out


def test_fs_read_stat_list_match_exe_golden(corpus, monkeypatch):
    rows = json.loads(open(G.FIXTURE, encoding="utf-8").read())
    live = _live_rows(corpus, monkeypatch)
    bad = [lv["label"] for lv, gd in zip(live, rows)
           if _semantic(lv["out"]) != _semantic(gd["out"])]
    assert not bad, f"{len(bad)} 场景与 exe golden 不等: {bad}"
