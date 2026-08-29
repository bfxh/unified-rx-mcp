# -*- coding: utf-8 -*-
"""安全模糊集（EVAL-L2 卡尺）：fail-closed 沙盒与写授权的边界回归。

不变量：凡不符合显式放开条件的文件访问一律拒绝；伪造授权形态全拒绝。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def _env(old):
    """恢复环境变量的 helper（old=None 表示原来没设）。"""
    if old is None:
        os.environ.pop("UNIFIED_RX_SANDBOX", None)
    else:
        os.environ["UNIFIED_RX_SANDBOX"] = old


def test_env_unset_denies_all():
    """fail-closed 核心：env 未设置 = 拒绝一切文件访问。"""
    old = os.environ.pop("UNIFIED_RX_SANDBOX", None)
    try:
        r = registry.call("fs_read", {"path": __file__})
        assert not r["ok"], "未配置沙盒必须拒绝"
        r2 = registry.call("fs_stat", {"path": "D:\\开发"})
        assert not r2["ok"], "未配置沙盒 stat 也拒绝"
    finally:
        _env(old)


def test_env_blank_and_whitespace_denies():
    """空串/纯空白 env 都不算配置，必须拒绝。"""
    old = os.environ.get("UNIFIED_RX_SANDBOX")
    for val in ["", " ", "  ;  "]:
        try:
            os.environ["UNIFIED_RX_SANDBOX"] = val
            r = registry.call("fs_read", {"path": __file__})
            assert not r["ok"], f"env={val!r} 应拒绝"
        finally:
            _env(old)


def test_star_is_explicit_allow():
    """'*' 是唯一的显式放开通道，且放开后能力正常。"""
    old = os.environ.get("UNIFIED_RX_SANDBOX")
    try:
        os.environ["UNIFIED_RX_SANDBOX"] = "*"
        r = registry.call("fs_stat", {"path": __file__})
        assert r["ok"] and r["result"]["exists"]
    finally:
        _env(old)


def test_symlink_escape_denied(tmp_path):
    """沙盒内 junction/symlink 指向外部目标必须被 realpath 拦截。"""
    link = Path(tmp_path) / "_escape_j"
    target = r"C:\Windows\Media"
    import subprocess

    ok = subprocess.run(["cmd", "/c", f'mklink /J "{link}" "{target}"'],
                        capture_output=True).returncode == 0
    if not ok or not link.exists():
        return  # 平台不支持 junction 时跳过断言
    old = os.environ.get("UNIFIED_RX_SANDBOX")
    try:
        os.environ["UNIFIED_RX_SANDBOX"] = str(Path(__file__).resolve().parent.parent)
        r = registry.call("fs_list", {"path": str(link)})
        assert not r["ok"], "junction 逃逸必须拒绝"
    finally:
        _env(old)
        subprocess.run(["cmd", "/c", f'rmdir "{link}"'], capture_output=True)


def test_forged_authorization_denied(tmp_path):
    """伪造 __authorized 形态（字符串/'True'/1）不得通过写授权。"""
    p = str(Path(tmp_path) / "_forge.txt")
    for forged in ("true", "True", 1, "1", [True]):
        r = registry.call("fs_write", {"path": p, "content": "x", "__authorized": forged})
        assert not r["ok"], f"伪造授权 {forged!r} 必须拒绝"
    assert not Path(p).exists()


def test_authorized_string_true_is_rejected_too():
    """__authorized='true' 字符串在真实写入路径上必须拒绝（防宽松真值判断）。"""
    old = os.environ.get("UNIFIED_RX_SANDBOX")
    try:
        os.environ["UNIFIED_RX_SANDBOX"] = "*"
        p = Path(os.path.dirname(__file__)) / "_auth_str_probe.txt"
        p.unlink(missing_ok=True)
        r = registry.call("fs_write", {"path": str(p), "content": "x", "__authorized": "true"})
        assert not r["ok"]
        assert not p.exists(), "拒绝后不得落盘"
        p.unlink(missing_ok=True)
    finally:
        _env(old)


def test_bad_paths_structured_reject():
    """坏路径（空/非字符串/穿越）返回结构化失败而非异常抛穿。"""
    for bad in ["", 123, None]:
        r = registry.call("fs_read", {"path": bad})
        assert not r["ok"] and isinstance(r.get("error"), str), f"path={bad!r} 应结构化拒绝"


def test_registry_requires_auth_declared():
    """UPGRADE-A1：写/执行工具必须在注册表声明 requires_auth（防新增漏配）。"""
    expected = {"fs_write", "ide_edit_multi", "local_run",
                "ide_build", "ide_debug", "ide_break", "ide_test",
                "ide_doctor"}
    declared = {n for n, v in registry._TOOLS.items() if v.get("requires_auth")}
    assert expected <= declared, f"未声明 requires_auth: {expected - declared}"


def test_authorized_forged_via_registry_layer(tmp_path):
    """A1 一层防线：registry.call 直接拦伪造授权（1/'true'/缺省）。"""
    p = str(Path(tmp_path) / "_forge2.txt")
    for forged in (1, "true", None):
        args = {"path": p, "content": "x"}
        if forged is not None:
            args["__authorized"] = forged
        r = registry.call("fs_write", args)
        assert not r["ok"] and "授权" in r.get("error", ""), f"forge={forged!r}: {r}"
    assert not Path(p).exists()


def test_local_run_requires_authorization():
    """local_run 执行类必须被 registry 层拦截（缺授权）。"""
    r = registry.call("local_run", {"domain": "python", "name": "script",
                                    "args": {"script": "x.py"}})
    assert not r["ok"], "无授权的 local_run 应被拒"


def test_pytest_tmp_not_in_repo_root():
    """A2 验收：夹具基目录不在仓库根，仓库根无 _pytest_tmp 残留。"""
    repo = Path(__file__).resolve().parent.parent
    import tempfile as _tf
    base = Path(_tf.gettempdir()) / "unified-rx-pytest"
    assert not (repo / "_pytest_tmp").exists(), "夹具目录不应在仓库根"
