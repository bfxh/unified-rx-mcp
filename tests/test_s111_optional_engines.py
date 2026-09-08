# -*- coding: utf-8 -*-
"""S111/S112 可选引擎契约：ast-grep 薄壳（校验/探测/清晰报错）+ SCIP 索引消费。

ast-grep 未装时验证"清晰报错 + 安装提示"；SCIP 用**合成索引**（手写 protobuf
编码）验证解析正确性——不依赖外部索引器产物。
"""
import os
import shutil

import pytest

import registry
import tools  # noqa: F401


# ---------- S111 ast_grep ----------

def test_ast_grep_schema():
    ent = registry._TOOLS["ast_grep"]
    assert ent["group"] == "scan"
    assert ent["schema"]["required"] == ["pattern", "path"]


def test_ast_grep_pattern_validation(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ast_grep", {"pattern": "a\x00b", "path": str(tmp_path)})
    assert r.get("ok") is False and "非法字符" in r["error"], r
    r2 = registry.call("ast_grep", {"pattern": "x" * 5000, "path": str(tmp_path)})
    assert r2.get("ok") is False and "4096" in r2["error"], r2


def test_ast_grep_sandbox(tmp_path):
    r = registry.call("ast_grep", {"pattern": "x", "path": r"C:\Windows"})
    assert r.get("ok") is False and "沙盒外" in r["error"], r


@pytest.mark.skipif(shutil.which("ast-grep") or shutil.which("sg"),
                    reason="本机装了 ast-grep——未装分支不可测")
def test_ast_grep_not_installed_clear_error(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("ast_grep", {"pattern": "x = $A", "path": str(tmp_path)})
    assert r.get("ok") is False, r
    err = r["error"]
    assert "未安装" in err and ("npm i -g" in err or "cargo install" in err), err


# ---------- S112 SCIP ----------

def _v(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _fld(no, payload):
    return _v((no << 3) | 2) + _v(len(payload)) + payload


def _fld_var(no, val):
    return _v((no << 3) | 0) + _v(val)


def _occ(line0, symbol, is_def):
    rng = _v(line0) + _v(0) + _v(line0) + _v(10)
    return _fld(1, rng) + _fld(2, symbol.encode()) + _fld_var(3, 1 if is_def else 0)


def _doc(path, occs):
    return _fld(1, path.encode()) + b"".join(_fld(2, o) for o in occs)


def _index(docs):
    return b"".join(_fld(2, d) for d in docs)


def test_scip_refs_parses_synthetic_index(tmp_path):
    data = _index([
        _doc("src/lib.rs", [_occ(4, "scip-rust pkg 0.1 `alpha()`", True),
                            _occ(9, "scip-rust pkg 0.1 `alpha()`", False)]),
        _doc("src/use.rs", [_occ(2, "scip-rust pkg 0.1 `alpha()`", False)]),
    ])
    idx = tmp_path / "index.scip"
    idx.write_bytes(data)
    r = registry.call("scip_refs", {"index_file": str(idx), "symbol": "alpha()"})
    assert r.get("ok"), r
    res = r["result"]
    assert res["engine"] == "scip" and res["documents"] == 2, res
    assert res["total_refs"] == 3, res
    by = {f["file"]: f for f in res["files"]}
    assert by["src/lib.rs"]["lines"] == [5, 10] and by["src/lib.rs"]["defs"] == 1, by
    assert by["src/use.rs"]["lines"] == [3], by


def test_scip_refs_missing_and_bad(tmp_path):
    r = registry.call("scip_refs", {"index_file": str(tmp_path / "nope.scip"),
                                    "symbol": "x"})
    assert r.get("ok") is False and "不存在" in r["error"], r
    bad = tmp_path / "bad.scip"
    bad.write_bytes(b"\xff\xff\xff\xff")
    r2 = registry.call("scip_refs", {"index_file": str(bad), "symbol": "x"})
    assert r2.get("ok") is False and "SCIP 解析失败" in r2["error"], r2


def test_scip_refs_symbol_not_found(tmp_path):
    idx = tmp_path / "i.scip"
    idx.write_bytes(_index([_doc("a.rs", [_occ(1, "sym-one", False)])]))
    r = registry.call("scip_refs", {"index_file": str(idx), "symbol": "no-such"})
    assert r.get("ok"), r
    assert r["result"]["total_refs"] == 0 and r["result"]["files"] == [], r


def test_scip_schema():
    ent = registry._TOOLS["scip_refs"]
    assert ent["group"] == "ide"
    assert set(ent["schema"]["properties"]) == {"index_file", "symbol", "k"}
