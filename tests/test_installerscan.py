"""installer_scan（伪造安装包扫描）契约测试。

口径（spec 见模块 docstring）：
- 判定纯函数 _verdict：签名层/版本资源/文件名/MOTW 四路证据 → 三档 + unknown；
- 名字启发式：破解/激活/keygen 关键词、双扩展名、下载副本（弱信号）；
- 组织名规范化：签名者 ↔ 自称厂商比对（后缀剥离 + 包含匹配）；
- Windows 集成：非 PE 文件 → unsigned 档；MOTW ADS 写读回路。
静态启发非保证——测试夹具独立构造，不依赖被测代码自证。
"""
import os
import sys

import pytest

from tools.installerscan import (
    V_FAKE,
    V_SUSPICIOUS,
    V_TRUSTED,
    V_UNKNOWN,
    _name_flags,
    _norm_org,
    _orgs_match,
    _parse_zone_id,
    _verdict,
    installer_scan,
)

_WIN = sys.platform == "win32"

# ---- _verdict：签名层 ----


def test_unsigned_claiming_known_vendor_is_fake():
    v, ind = _verdict("unsigned", {"CompanyName": "Microsoft Corporation"}, [], None, True)
    assert v == V_FAKE
    assert any(i["flag"] == "impersonated-vendor" for i in ind)


def test_modified_signature_is_fake():
    v, ind = _verdict("modified", {}, [], None, True)
    assert v == V_FAKE
    assert any(i["flag"] == "signature-modified" for i in ind)


def test_revoked_signature_is_fake():
    v, _ = _verdict("revoked", {}, [], None, True)
    assert v == V_FAKE


def test_plain_unsigned_is_suspicious():
    v, ind = _verdict("unsigned", {}, [], None, True)
    assert v == V_SUSPICIOUS
    assert any(i["flag"] == "signature-unsigned" for i in ind)


def test_unsigned_from_internet_is_suspicious():
    v, ind = _verdict("unsigned", {}, [], 3, True)
    assert v == V_SUSPICIOUS
    assert any(i["flag"] == "from-internet-unsigned" for i in ind)


def test_trusted_with_matching_vendor_is_clean():
    v, _ = _verdict("trusted", {"CompanyName": "Example Corp", "_signer": "Example Corp"},
                    [], None, True)
    assert v == V_TRUSTED


def test_trusted_with_mismatched_signer_is_suspicious():
    v, ind = _verdict("trusted", {"CompanyName": "Microsoft Corporation",
                                  "_signer": "Joe Random"}, [], None, True)
    assert v == V_SUSPICIOUS
    assert any(i["flag"] == "signer-vendor-mismatch" for i in ind)


def test_expired_signature_is_suspicious():
    v, _ = _verdict("expired", {}, [], None, True)
    assert v == V_SUSPICIOUS


# ---- _verdict：非 Windows（无签名层）----

def test_non_windows_without_metadata_is_unknown():
    v, _ = _verdict(None, {}, [], None, False)
    assert v == V_UNKNOWN


def test_non_windows_still_flags_suspect_name():
    v, ind = _verdict(None, {}, [{"flag": "suspect-keyword", "detail": "x"}], None, False)
    assert v == V_FAKE
    assert ind


# ---- _name_flags ----


def test_crack_keyword_flagged():
    flags = _name_flags("Photoshop 2024 破解版 Setup.exe")
    assert any(f["flag"] == "suspect-keyword" for f in flags)


def test_keygen_flagged():
    flags = _name_flags("keygen.exe")
    assert any(f["flag"] == "suspect-keyword" for f in flags)


def test_double_extension_flagged():
    flags = _name_flags("invoice.pdf.exe")
    assert any(f["flag"] == "double-extension" for f in flags)


def test_clean_name_has_no_flags():
    assert _name_flags("setup.exe") == []


def test_copy_suffix_is_weak_flag():
    flags = _name_flags("setup (1).exe")
    assert any(f["flag"] == "download-copy" for f in flags)


# ---- 组织名规范化 ----


def test_norm_org_strips_suffixes():
    assert _norm_org("Example Corporation") == _norm_org("example  corp")


def test_orgs_match_subset():
    assert _orgs_match("Example", "Example Corporation")


def test_orgs_mismatch():
    assert not _orgs_match("Joe Random", "Microsoft Corporation")


def test_orgs_match_empty_is_neutral():
    assert _orgs_match("", "Microsoft Corporation")


# ---- MOTW 文本解析 ----


def test_parse_zone_id():
    assert _parse_zone_id("[ZoneTransfer]\r\nZoneId=3\r\n") == 3
    assert _parse_zone_id("[ZoneTransfer]\r\nZoneId=0\r\n") == 0
    assert _parse_zone_id("garbage") is None
    assert _parse_zone_id("") is None


# ---- Windows 集成（工具整体回路）----

@pytest.mark.skipif(not _WIN, reason="MOTW/Authenticode 仅 Windows")
def test_scan_non_pe_exe_reports_unsigned(tmp_path):
    target = tmp_path / "notape.exe"
    target.write_bytes(b"this is not a PE file at all" * 10)
    out = installer_scan(str(target))
    assert out.get("ok") is True
    entry = out["files"][0]
    assert entry["signature"]["status"] in ("unsigned", "unreadable", "hresult-0x80092002")
    assert entry["verdict"] in (V_SUSPICIOUS, V_FAKE, V_UNKNOWN)


@pytest.mark.skipif(not _WIN, reason="MOTW 仅 NTFS")
def test_scan_reads_motw(tmp_path):
    target = tmp_path / "downloaded_setup.exe"
    target.write_bytes(b"MZ" + b"\x00" * 64)
    with open(f"{target}:Zone.Identifier", "w", encoding="ascii") as ads:
        ads.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    out = installer_scan(str(target))
    assert out.get("ok") is True
    entry = out["files"][0]
    assert entry["motw_zone"] == 3


def test_scan_missing_path_is_error(tmp_path):
    out = installer_scan(str(tmp_path / "nope.exe"))
    assert "error" in out


def test_scan_dir_finds_executables(tmp_path):
    (tmp_path / "a.exe").write_bytes(b"MZ" + b"\x00" * 32)
    (tmp_path / "b.txt").write_text("not an executable")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.msi").write_bytes(b"MZ" + b"\x00" * 32)
    out = installer_scan(str(tmp_path))
    assert out.get("ok") is True
    names = {os.path.basename(e["path"]) for e in out["files"]}
    assert names == {"a.exe", "c.msi"}
