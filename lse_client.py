"""lse_client — LSE 引擎的 Python 客户端（subprocess JSON 协议）。

调用 Rust lse-engine（~/.unified-rx/lse-state.json 持久化）：
- delta_update_lesson(id, delta): 教训效用分更新（Delta 奖励）
- delta_update_rule(id, delta, adopted): 规则权重更新（自适应）
- ucb_select(parent, children, c): UCB 树搜索分支选择
- ucb_backprop(id, reward): 树节点结果回流
- experience_store(model, ctx, delta, summary): 经验入库
- experience_match(ctx, limit): 按上下文指纹匹配经验

零第三方依赖（subprocess + json）。lse-engine exe 路径：同目录 ../lse-engine/target/release/lse-engine.exe
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_ENGINE_CANDIDATES = [
    # 同仓库 lse-engine 子目录 release 构建（unified-rx/lse-engine/target/release/）
    Path(__file__).resolve().parent / "lse-engine" / "target" / "release" / "lse-engine.exe",
    Path(__file__).resolve().parent / "lse-engine" / "target" / "release" / "lse-engine",
    # 仓库根（mcp-servers/ 布局）lse-engine
    Path(__file__).resolve().parent.parent / "unified-rx" / "lse-engine" / "target" / "release" / "lse-engine.exe",
    # 环境变量覆盖
    Path(os.environ.get("LSE_ENGINE", "")) if os.environ.get("LSE_ENGINE") else None,
]

_TIMEOUT = 5.0


def _engine_path() -> Path | None:
    for c in _ENGINE_CANDIDATES:
        if c is not None and c.exists():
            return c
    return None


def _call(cmd: str, payload: dict) -> dict:
    """调用 lse-engine，返回 {ok, result|error}。引擎不可用时返回 ok:false。"""
    exe = _engine_path()
    if exe is None:
        return {"ok": False, "error": "lse-engine 未构建（lse-engine/target/release/）"}
    try:
        line = json.dumps({"cmd": cmd, "payload": payload}, ensure_ascii=False)
        proc = subprocess.run(
            [str(exe)], input=line, capture_output=True, text=True,
            timeout=_TIMEOUT, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return {"ok": False, "error": f"lse-engine exit {proc.returncode}: {proc.stderr[:200]}"}
        out = (proc.stdout or "").strip().splitlines()
        return json.loads(out[-1]) if out else {"ok": False, "error": "empty output"}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def engine_available() -> bool:
    return _engine_path() is not None


def delta_update_lesson(lesson_id: str, delta: float, threshold: float = 0.1) -> dict:
    return _call("delta_update", {"kind": "lesson", "id": lesson_id, "delta": delta, "threshold": threshold})


def delta_update_rule(rule_id: str, delta: float, adopted: bool = True) -> dict:
    return _call("delta_update", {"kind": "rule", "id": rule_id, "delta": delta, "adopted": adopted})


def ucb_select(parent: str, children: list, c: float = 1.41) -> dict:
    return _call("ucb_select", {"parent": parent, "children": children, "c": c})


def ucb_backprop(node_id: str, reward: float) -> dict:
    return _call("ucb_backprop", {"id": node_id, "reward": reward})


def experience_store(model: str, ctx: str, delta: float, summary: str) -> dict:
    return _call("experience_store", {"model": model, "ctx": ctx, "delta": delta, "summary": summary})


def experience_match(ctx: str, limit: int = 5) -> dict:
    return _call("experience_match", {"ctx": ctx, "limit": limit})


def state_get() -> dict:
    return _call("state_get", {})
