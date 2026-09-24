"""S141 契约：session-guard 会话烧量哨兵钩子（zcode-breaker 插件）。

与宿主行为同形：子进程 + stdin JSON + stdout 严格信封；隔离 HOME（USERPROFILE，
Windows 下 expanduser 优先看它）与 TEMP，绝不读到真实 rollout / 状态文件。
文件路径全用字面量组件构造（与钩子同纪律）。
"""
import json
import os
import pathlib
import subprocess
import sys

GUARD = (pathlib.Path(__file__).resolve().parent.parent / "plugins" / "urx-marketplace"
         / "zcode-breaker" / "hooks" / "session_guard_hook.py")


def _env(tmp_path, extra=None, with_rollout=False):
    env = dict(os.environ)
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)          # Windows：expanduser("~") 优先读它
    env.pop("ZCODE_SESSION_GUARD", None)
    if with_rollout:
        (tmp_path / ".zcode" / "cli" / "rollout").mkdir(parents=True, exist_ok=True)
    if extra:
        env.update(extra)
    return env


def _run(mode, ev, env):
    cp = subprocess.run([sys.executable, str(GUARD), mode],
                        input=json.dumps(ev).encode("utf-8"),
                        capture_output=True, env=env)
    return cp.returncode, (cp.stdout or b"").decode("utf-8", "replace")


def test_start_emits_hygiene_rules(tmp_path):
    rc, out = _run("start", {}, _env(tmp_path, with_rollout=True))
    assert rc == 0, out
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "会话卫生" in data["hookSpecificOutput"]["additionalContext"]


def test_prompt_warns_on_tier_cross_then_silent(tmp_path):
    env = _env(tmp_path, with_rollout=True)
    big = tmp_path / ".zcode" / "cli" / "rollout" / "model-io-s1.jsonl"
    big.write_bytes(b"x" * 15_100_000)          # 15.1MB → 第 1 档
    rc, out = _run("prompt", {"session_id": "s1"}, env)
    assert rc == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "烧量哨兵" in ctx and "第 1 档" in ctx
    rc2, out2 = _run("prompt", {"session_id": "s1"}, env)
    assert rc2 == 0 and out2.strip() == "", out2        # 同档不重复


def test_prompt_silent_without_big_session(tmp_path):
    env = _env(tmp_path, with_rollout=True)
    small = tmp_path / ".zcode" / "cli" / "rollout" / "model-io-small.jsonl"
    small.write_bytes(b"x" * 3000)              # 低于首档
    rc, out = _run("prompt", {"session_id": "small"}, env)
    assert rc == 0 and out.strip() == "", out


def test_bypass_env(tmp_path):
    env = _env(tmp_path, extra={"ZCODE_SESSION_GUARD": "off"}, with_rollout=True)
    big = tmp_path / ".zcode" / "cli" / "rollout" / "model-io-s1.jsonl"
    big.write_bytes(b"x" * 99_000_000)
    rc, out = _run("prompt", {"session_id": "s1"}, env)
    assert rc == 0 and out.strip() == "", out


def test_garbage_stdin_passes(tmp_path):
    cp = subprocess.run([sys.executable, str(GUARD), "prompt"],
                        input=b"not json", capture_output=True, env=_env(tmp_path))
    assert cp.returncode == 0
    assert not (cp.stdout or b"").strip()
