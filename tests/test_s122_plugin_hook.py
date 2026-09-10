# -*- coding: utf-8 -*-
"""S122 ZCode 插件契约：plugins/urx-marketplace/zcode-breaker 的钩子行为。

钩子以子进程方式被 ZCode 调用：stdin 收事件 JSON，退出码 2 = 阻断（stderr 作为
阻断原因），0 = 放行；UserPromptSubmit 的告警走 stdout 的严格 JSON 信封。
本测试用真实子进程 + 隔离 TEMP 验证，与宿主行为同形。
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

HOOK = (pathlib.Path(__file__).resolve().parent.parent / "plugins" / "urx-marketplace"
        / "zcode-breaker" / "hooks" / "breaker_hook.py")


def _run(mode, limit, ev, tmp_path, extra_env=None, window=300, cooldown=60):
    env = dict(os.environ)
    # 三个变量都要给：Windows 下 tempfile.gettempdir() 依次看 TMPDIR→TEMP→TMP，
    # 父进程若已有 TMPDIR（Git Bash 常见）会压过 TEMP，状态就落进真实临时目录。
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(tmp_path)
    env.pop("ZCODE_BREAKER", None)
    if extra_env:
        env.update(extra_env)
    cp = subprocess.run([sys.executable, str(HOOK), mode, str(limit), str(window), str(cooldown)],
                        input=json.dumps(ev).encode("utf-8"), capture_output=True, env=env)
    return cp.returncode, (cp.stderr or b"").decode("utf-8", "replace"), \
        (cp.stdout or b"").decode("utf-8", "replace")


def _ev(session="s1", tool="Bash", tin=None, **kw):
    d = {"session_id": session, "tool_name": tool,
         "tool_input": tin if tin is not None else {"command": "ls -la"}}
    d.update(kw)
    return d


def test_pre_trips_after_limit(tmp_path):
    for i in range(3):
        rc, err, _ = _run("pre", 3, _ev(), tmp_path)
        assert rc == 0, (i, rc, err)
    rc, err, _ = _run("pre", 3, _ev(), tmp_path)
    assert rc == 2 and "熔断" in err, (rc, err)
    # 冷却期内仍阻断
    rc, err, _ = _run("pre", 3, _ev(), tmp_path)
    assert rc == 2, (rc, err)


def test_different_args_pass(tmp_path):
    for _ in range(3):
        _run("pre", 3, _ev(), tmp_path)
    rc, _, _ = _run("pre", 3, _ev(tin={"command": "pwd"}), tmp_path)
    assert rc == 0


def test_sessions_isolated(tmp_path):
    for _ in range(3):
        _run("pre", 3, _ev(session="a"), tmp_path)
    assert _run("pre", 3, _ev(session="a"), tmp_path)[0] == 2
    assert _run("pre", 3, _ev(session="b"), tmp_path)[0] == 0


def test_post_streak_trips_next_pre(tmp_path):
    for _ in range(3):
        rc, _, _ = _run("post", 3, _ev(tool="Read", tin={"file_path": "a.txt"},
                                        tool_response={"content": "same"}), tmp_path)
        assert rc == 0
    rc, err, _ = _run("pre", 3, _ev(tool="Read", tin={"file_path": "a.txt"}), tmp_path)
    assert rc == 2 and "熔断" in err, (rc, err)


def test_prompt_warns_not_blocks(tmp_path):
    for _ in range(2):
        rc, _, out = _run("prompt", 2, {"session_id": "s", "prompt": "继续"}, tmp_path)
        assert rc == 0 and out.strip() == "", out
    rc, _, out = _run("prompt", 2, {"session_id": "s", "prompt": "继续"}, tmp_path)
    assert rc == 0, rc                      # 不阻断用户输入
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "重复" in data["hookSpecificOutput"]["additionalContext"]


def test_bypass_env(tmp_path):
    for _ in range(10):
        rc, _, _ = _run("pre", 1, _ev(), tmp_path, extra_env={"ZCODE_BREAKER": "off"})
        assert rc == 0


def test_garbage_stdin_passes(tmp_path):
    env = dict(os.environ)
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(tmp_path)
    env.pop("ZCODE_BREAKER", None)
    cp = subprocess.run([sys.executable, str(HOOK), "pre", "1", "300", "60"],
                        input=b"not json at all", capture_output=True, env=env)
    assert cp.returncode == 0


def test_state_file_created_under_temp(tmp_path):
    _run("pre", 5, _ev(), tmp_path)
    state = tmp_path / "zcode-breaker" / "state.json"
    assert state.is_file(), list(tmp_path.iterdir())
    data = json.loads(state.read_text(encoding="utf-8"))
    assert "sessions" in data and data["sessions"]


def test_hooks_manifest_matches_script(tmp_path):
    """hooks.json 必须注册七个事件里的四个，且都指向本脚本（防手改漂移）。"""
    manifest = json.loads((HOOK.parent / "hooks.json").read_text(encoding="utf-8"))
    events = set(manifest["hooks"])
    assert events == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit"}
    for ev, entries in manifest["hooks"].items():
        for entry in entries:
            for h in entry["hooks"]:
                assert h["type"] == "process" and h["command"] == "python"
                assert any("breaker_hook.py" in a for a in h["args"]), (ev, h)
