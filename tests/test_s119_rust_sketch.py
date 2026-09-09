# -*- coding: utf-8 -*-
"""S119 Rust sketch 契约：rx-scan sketch 批量指纹 vs Python oracle + near_dupes 三档引擎。

背景（实测）：n-gram bottom-k 的 GPU 路径打的是纯 Python 基线；原生 Rust（std::thread
分块）16MB 5.4ms vs GPU 32.5ms、300×20KB 约 15ms vs GPU 651ms。故 near_dupes 指纹
默认走 `rx-scan sketch`（一次进程调用批量），GPU/CPU 保留为回落路径并**如实上报**。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import gpu, neardupes

_HAS_EXE = neardupes._rx_scan_exe() is not None
_EXE_HINT = "rx-scan.exe 未构建（cargo build --release）"


def _mk_files(tmp_path):
    a = os.urandom(40_000)
    b = bytearray(a)
    b[20_000:20_008] = os.urandom(8)
    (tmp_path / "a.bin").write_bytes(a)
    (tmp_path / "b.bin").write_bytes(bytes(b))
    (tmp_path / "r.bin").write_bytes(os.urandom(40_000))
    return [str(tmp_path / n) for n in ("a.bin", "b.bin", "r.bin")]


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_rust_sketch_matches_python_oracle(tmp_path):
    files = _mk_files(tmp_path)
    table, errors = neardupes._rust_sketch(files, 4, 32)
    assert errors == {}, errors
    for fp in files:
        want = gpu.ngram_bottomk_cpu(open(fp, "rb").read(), 4, 32)
        assert table[fp] == want, fp


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_rust_sketch_reports_unreadable_file(tmp_path):
    missing = str(tmp_path / "nope.bin")
    table, errors = neardupes._rust_sketch([missing], 4, 8)
    assert table == {} and missing in errors, (table, errors)


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_near_dupes_rust_engine_reported(tmp_path):
    _mk_files(tmp_path)
    r = registry.call("near_dupes", {"path": str(tmp_path), "engine": "rust"})
    assert r.get("ok"), r
    res = r["result"]
    assert res["sketch_engine"] == {"rust": 3, "gpu": 0, "cpu": 0}, res["sketch_engine"]
    assert res["sketch_fallback"] is None, res
    assert [sorted(c) for c in res["clusters"]] == [
        [str(tmp_path / "a.bin"), str(tmp_path / "b.bin")]], res["clusters"]


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_rust_and_cpu_engines_agree(tmp_path):
    _mk_files(tmp_path)
    a = registry.call("near_dupes", {"path": str(tmp_path), "engine": "rust"})
    b = registry.call("near_dupes", {"path": str(tmp_path), "engine": "cpu"})
    assert a.get("ok") and b.get("ok")
    assert a["result"]["pairs"] == b["result"]["pairs"]
    assert a["result"]["clusters"] == b["result"]["clusters"]


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_auto_prefers_rust(tmp_path):
    _mk_files(tmp_path)
    r = registry.call("near_dupes", {"path": str(tmp_path)})
    assert r.get("ok"), r
    assert r["result"]["sketch_engine"]["rust"] == 3, r["result"]["sketch_engine"]


def test_rust_missing_falls_back_and_reports(tmp_path, monkeypatch):
    _mk_files(tmp_path)
    monkeypatch.setattr(neardupes, "_rx_scan_exe", lambda: None)
    r = registry.call("near_dupes", {"path": str(tmp_path), "engine": "rust"})
    assert r.get("ok"), r
    res = r["result"]
    assert res["sketch_engine"]["rust"] == 0, res["sketch_engine"]
    assert res["sketch_engine"]["gpu"] + res["sketch_engine"]["cpu"] == 3, \
        res["sketch_engine"]
    assert "rx-scan.exe 不存在" in (res["sketch_fallback"] or ""), res
    assert res["clusters"], res


def test_weird_paths_roundtrip_via_rust(tmp_path):
    """空格/中文/特殊字符路径经帧流传给 exe 不丢字（无 exe 时跳过）。"""
    if not _HAS_EXE:
        pytest.skip(_EXE_HINT)
    odd = tmp_path / "带 中文 空格 #1.bin"
    odd.write_bytes(os.urandom(3000))
    table, errors = neardupes._rust_sketch([str(odd)], 4, 8)
    assert errors == {} and len(table[str(odd)]) == 8, (table, errors)
