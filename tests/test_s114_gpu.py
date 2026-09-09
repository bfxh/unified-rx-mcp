# -*- coding: utf-8 -*-
"""S114/S115 GPU 支持契约：OpenCL 内核 vs CPU oracle / 交叉点选路 / 降级 / 文件扫描。

环境相关项（是否有 GPU）用 skipif 分流；降级路径用 monkeypatch 强制。
"""
import hashlib
import os
import pytest

import registry
import tools  # noqa: F401
from tools import filescan
from tools import gpu

_HAS_GPU = gpu.status().get("available") is True
# EICAR 测试串（业界标准可验证样例，非真实恶意样本）——分片拼接，避免源码里成串
_EICAR = (b"X5O!P%@AP[4" + b"\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")


def test_status_shape():
    st = gpu.status()
    assert "available" in st
    if st["available"]:
        assert st["runtime"] and st["platforms"]
        assert any(p["devices"] for p in st["platforms"])
    else:
        assert st.get("reason"), "不可用时必须给出原因（不静默）"


def test_entropy_math():
    assert gpu.entropy([100, 0] + [0] * 254, 100) == 0.0
    assert abs(gpu.entropy([1] * 256, 256) - 8.0) < 1e-9


def test_pick_mode_crossover():
    assert gpu.pick_mode("byte_hist_bytes", 512 * 1024) == "cpu"
    assert gpu.pick_mode("byte_hist_bytes", 2 * 1024 * 1024) == "gpu"
    assert gpu.pick_mode("literal_scan_bytes", 100 * 1024 * 1024) == "cpu"
    assert gpu.pick_mode("byte_hist_bytes", 1, mode="gpu") == "gpu"
    assert gpu.pick_mode("byte_hist_bytes", 1 << 30, mode="cpu") == "cpu"


def test_degrade_when_runtime_missing(monkeypatch):
    monkeypatch.setattr(gpu, "_lib_name", lambda: "no_such_opencl_runtime_xyz.dll")
    monkeypatch.setattr(gpu, "_CL", None)
    st = gpu.status()
    assert st["available"] is False and "加载失败" in st["reason"], st
    with pytest.raises(gpu.GpuError):
        gpu.byte_hist_gpu(b"abc")
    monkeypatch.setattr(gpu, "_CL", None)


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_hist_gpu_matches_cpu_oracle():
    data = os.urandom(2_400_000)
    hg = gpu.byte_hist_gpu(data)
    hc = gpu.byte_hist_cpu(data)
    assert hg == hc, "GPU 直方图必须与 CPU 逐位一致"
    assert sum(hg) == len(data)


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_literal_gpu_matches_cpu_oracle():
    data = (b"alpha beta gamma delta " * 5000) + b"SIG_MARKER"
    pats = [b"SIG_MARKER", b"gamma delta", b"nope-xyz"]
    assert gpu.literal_scan_gpu(data, pats) == gpu.literal_scan_cpu(data, pats)


# ---------- file_scan ----------

def test_file_scan_eicar_packed_and_clean(tmp_path):
    (tmp_path / "eicar.txt").write_bytes(_EICAR)
    (tmp_path / "plain.txt").write_text("hello world\n", encoding="utf-8")
    big = tmp_path / "packed.bin"
    big.write_bytes(os.urandom(2 * 1024 * 1024))
    r = registry.call("file_scan", {"path": str(tmp_path)})
    assert r.get("ok"), r
    res = r["result"]
    assert res["scanned"] == 3, res
    by = {os.path.basename(f["file"]): f for f in res["findings"]}
    assert "eicar.txt" in by and by["eicar.txt"]["signatures"], by
    assert by["packed.bin"]["packed"] is True and by["packed.bin"]["entropy"] > 7.0, by
    assert "plain.txt" not in by, by
    assert "非杀毒软件" in res["note"], res["note"]


def test_file_scan_hash_blacklist(tmp_path):
    f = tmp_path / "m.bin"
    payload = b"some payload for hashing"
    f.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    r = registry.call("file_scan", {"path": str(f), "hashes": [sha]})
    assert r.get("ok"), r
    assert r["result"]["findings"][0]["hash_hit"] is True, r


def test_file_scan_custom_signature(tmp_path):
    (tmp_path / "x.txt").write_text("contains CUSTOM_SIG_7 here\n", encoding="utf-8")
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "signatures": ["CUSTOM_SIG_7"]})
    assert r.get("ok"), r
    assert r["result"]["findings"][0]["signatures"] == ["CUSTOM_SIG_7"], r


def test_file_scan_sandbox():
    r = registry.call("file_scan", {"path": r"C:\Windows"})
    assert r.get("ok") is False and "沙盒外" in r["error"], r


def test_gpu_status_tool_contract():
    ent = registry._TOOLS["gpu_status"]
    assert ent["group"] == "meta"
    assert ent["schema"]["required"] == []
    r = registry.call("gpu_status", {})
    assert r.get("ok"), r
    assert "available" in r["result"], r
