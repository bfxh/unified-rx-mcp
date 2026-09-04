# -*- coding: utf-8 -*-
"""S75：权力面盘点（继承 S73 深扫后的全面核查）四处收口的回归测试。

1. blender_verify：screenshot_path 原样拼进 PowerShell 单引号字符串——路径含 '
   即可逃逸注入任意 PS 命令；且全屏截屏=隐私面、spawn powershell=执行面。
   → requires_auth + screenshot_path 过沙盒 + _ps_quote 转义
2. process：taskkill /F /IM|/PID 可杀任意进程（含宿主自身），argv 形式无 shell
   注入但破坏性动作无门。→ requires_auth
3. backup：action=backup 把任意 root 全量打包成 zip（S73 app_clone 同级隐私面），
   root 只 abspath 不过沙盒。→ requires_auth + root 过沙盒
4. engine_query：root 喂给 codegraph CLI（-p）与 BM25，不钳沙盒。
   → root 过沙盒（S73 dep_graph/module_stability 同纪律）
5. capability_manifest：高权限清单动态生成（S72b 注入 × requires_auth 反向读出）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
import tools  # noqa: E402,F401  注册全部

越界 = "越界"
授权 = "授权"


def _declared(name):
    """S72b 契约：requires_auth 工具在 list_tools 里必须声明 __authorized。"""
    for t in registry.list_tools():
        if t["name"] == name:
            props = t["inputSchema"].get("properties") or {}
            req = t["inputSchema"].get("required") or []
            return "__authorized" in props and "__authorized" in req
    return False


# ---------- blender_verify：授权门 + PS 注入 + 沙盒 ----------

def test_blender_verify_requires_auth():
    r = registry.call("blender_verify", {})
    assert r["ok"] is False and 授权 in r["error"], r


def test_blender_verify_declares_authorized_in_schema():
    assert _declared("blender_verify")


def test_blender_verify_screenshot_path_outside_sandbox_denied(tmp_path):
    r = registry.call("blender_verify", {"screenshot_path": r"C:/Windows/x.png",
                                         "__authorized": True})
    assert r["ok"] is False and 越界 in r["error"], r


def test_blender_verify_ps_quote_escapes_single_quote():
    from tools.game import _ps_quote
    assert _ps_quote("x'y.png") == "x''y.png"
    # 注入尝试：每个 ' 都必须成对转义——嵌回 $bmp.Save('...') 后
    # 字符串只在结尾终止，注入串沦为字面文件名
    esc = _ps_quote("a.png'); Remove-Item -Recurse C:\\; ('")
    assert "'" not in esc.replace("''", ""), f"残留孤立引号: {esc}"


def test_blender_verify_no_blender_early_return(tmp_path, monkeypatch):
    """授权 + 沙盒内路径走通前置流程；无 Blender 时在截图前干净返回（mock 掉 tasklist）。"""
    import tools.game as g

    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    shot = tmp_path / "shot.png"
    r = registry.call("blender_verify", {"screenshot_path": str(shot),
                                         "__authorized": True})
    # handler 返回 ok:False 时 registry 再包一层：payload 在 r["result"]
    note = (r.get("result") or {}).get("note") or r.get("note") or ""
    assert r["ok"] is False and "未运行" in note, r
    assert len(calls) == 1, "只应调 tasklist 一次，不得触发截图"


# ---------- process：授权门 ----------

def test_process_requires_auth():
    r = registry.call("process", {"action": "kill", "name": "explorer.exe"})
    assert r["ok"] is False and 授权 in r["error"], r


def test_process_declares_authorized_in_schema():
    assert _declared("process")


def test_process_list_authorized_ok():
    r = registry.call("process", {"action": "list", "__authorized": True})
    assert r["ok"] is True and r["result"]["count"] > 0, r


# ---------- backup：授权门 + root 沙盒 ----------

def test_backup_requires_auth(tmp_path):
    r = registry.call("backup", {"root": str(tmp_path), "action": "list"})
    assert r["ok"] is False and 授权 in r["error"], r


def test_backup_declares_authorized_in_schema():
    assert _declared("backup")


def test_backup_root_outside_sandbox_denied():
    r = registry.call("backup", {"root": r"C:/Windows", "action": "backup",
                                 "__authorized": True})
    assert r["ok"] is False and 越界 in r["error"], r


def test_backup_roundtrip_inside_sandbox(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("backup", {"root": str(proj), "action": "backup",
                                 "__authorized": True})
    assert r["ok"] is True and r["result"]["files"] == 1, r
    assert Path(r["result"]["file"]).exists()
    lst = registry.call("backup", {"root": str(proj), "action": "list",
                                   "__authorized": True})
    assert lst["ok"] is True and len(lst["result"]["snapshots"]) == 1, lst


# ---------- engine_query：root 沙盒 ----------

def test_engine_query_root_outside_sandbox_denied():
    r = registry.call("engine_query", {"query": "x", "root": r"C:/Windows"})
    assert r["ok"] is False and 越界 in r["error"], r


# ---------- capability_manifest：高权限段动态生成 ----------

def test_manifest_lists_high_privilege_tools():
    r = registry.call("capability_manifest", {})
    assert r["ok"] is True, r
    gated = set(r["result"]["高权限"]["工具"])
    # S75 新挂门 + S73 已挂门 + 既有写/执行面，一个都不能漏
    for name in ("blender_verify", "process", "backup", "code_coverage",
                 "app_clone", "fs_write", "local_run"):
        assert name in gated, f"{name} 应在高权限清单: {gated}"
