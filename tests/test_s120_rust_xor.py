# -*- coding: utf-8 -*-
"""S120 Rust 异或枚举契约：rx-scan xor vs Python oracle + file_scan 三档引擎如实上报。

实测交叉点（单文件 / min-of-3）：64KB CPU 最优、128-256KB GPU 最优、≥512KB Rust 最优
（16MB：rust 485ms / gpu 1279ms / cpu 3763ms）。故 auto 选路：≥256KB rust →
≥128KB gpu → cpu；强制引擎不可用时按交叉点回落并如实回传 `xor_fallback`。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import filescan, gpu

_HAS_EXE = filescan._rx_scan_exe() is not None
_EXE_HINT = "rx-scan.exe 未构建（cargo build --release）"
_CRIB = b"MZ\x90\x00\x03\x00\x00\x00"
_KEY = 0x5A


def _mk_obf(path, nbytes, key=_KEY):
    payload = bytes(b ^ key for b in (_CRIB + os.urandom(max(0, nbytes - len(_CRIB)))))
    with open(path, "wb") as f:
        f.write(payload)
    return payload


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_rust_xor_matches_python_oracle(tmp_path):
    fp = str(tmp_path / "obf.bin")
    data = _mk_obf(fp, 300_000)
    got = filescan._rust_xor(fp, _CRIB)
    assert got == gpu.xor_crib_scan_cpu(data, _CRIB), (got[:5],)


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_rust_xor_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        filescan._rust_xor(str(tmp_path / "nope.bin"), _CRIB)


@pytest.mark.skipif(not _HAS_EXE, reason=_EXE_HINT)
def test_file_scan_xor_uses_rust_for_large_file(tmp_path):
    _mk_obf(str(tmp_path / "big.bin"), 512 * 1024)
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:" + _CRIB.hex()})
    assert r.get("ok"), r
    res = r["result"]
    assert res["xor_engine"]["rust"] == 1, res["xor_engine"]
    f = res["findings"][0]
    assert f["xor_engine"] == "rust" and f["xor_keys"] == [{"key": _KEY, "count": 1}], f
    assert res["xor_fallback"] is None, res


def test_file_scan_xor_small_file_uses_cpu(tmp_path):
    _mk_obf(str(tmp_path / "small.bin"), 8 * 1024)
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:" + _CRIB.hex()})
    assert r.get("ok"), r
    assert r["result"]["xor_engine"]["cpu"] == 1, r["result"]["xor_engine"]


def test_file_scan_xor_missing_exe_falls_back_and_reports(tmp_path, monkeypatch):
    _mk_obf(str(tmp_path / "big.bin"), 512 * 1024)
    monkeypatch.setattr(filescan, "_rx_scan_exe", lambda: None)
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:" + _CRIB.hex(),
                                    "xor_engine": "rust"})
    assert r.get("ok"), r
    res = r["result"]
    assert res["xor_engine"]["rust"] == 0, res["xor_engine"]
    assert res["xor_engine"]["gpu"] + res["xor_engine"]["cpu"] == 1, res["xor_engine"]
    assert "rx-scan.exe 不存在" in (res["xor_fallback"] or ""), res
    assert res["findings"][0]["xor_keys"] == [{"key": _KEY, "count": 1}], res["findings"]


def test_file_scan_xor_forced_cpu(tmp_path):
    _mk_obf(str(tmp_path / "big.bin"), 512 * 1024)
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:" + _CRIB.hex(),
                                    "xor_engine": "cpu"})
    assert r.get("ok"), r
    assert r["result"]["xor_engine"]["cpu"] == 1, r["result"]["xor_engine"]
    assert r["result"]["findings"][0]["xor_keys"] == [{"key": _KEY, "count": 1}]
