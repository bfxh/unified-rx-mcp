"""lse_client — LSE 引擎的 Python 客户端（subprocess JSON 协议）。

调用 Rust lse-engine（~/.unified-rx/lse-state.json 持久化）：
- delta_update_lesson(id, delta): 教训效用分更新（Delta 奖励）
- delta_update_rule(id, delta, adopted): 规则权重更新（自适应）
- ucb_select(parent, children, c): UCB 树搜索分支选择
- ucb_backprop(id, reward): 树节点结果回流
- experience_store(model, ctx, delta, summary): 经验入库
- experience_match(ctx, limit): 按上下文指纹匹配经验

零第三方依赖（subprocess + json）。lse-engine exe 路径：同目录 ../lse-engine/target/release/lse-engine.exe

P1c 升级（2026-08-12，抄 mem0 自动提取 + Letta 三层）：
- lesson_store_tiered: 三层存教训（core/work/archive）
- auto_extract_lessons: 规则版自动教训提取（信号词匹配，零 LLM）
"""

import hashlib
import json
import os
import re
import subprocess
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


def lesson_recall(lesson_id: str) -> dict:
    """查询单条教训（不触发 recall_count++，防查询污染枢纽信号）。"""
    return _call("lesson_recall", {"id": lesson_id})


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


# ── P1c 进化记忆升级（2026-08-12，抄 mem0 自动提取 + Letta 三层）──
# 三层：core(核心教训，长期) / work(工作教训，短期) / archive(归档)
_LSE_TIERS = ("core", "work", "archive")
# 每条教训 ID 前缀分层：core_ / work_ / archive_
_TIER_PREFIX = {"core": "core_", "work": "work_", "archive": "archive_"}


def lesson_store_tiered(tier: str, content: str, delta: float = 0.0,
                        threshold: float = 0.1) -> dict:
    """分层存教训：core/work/archive 三级（Letta 启发）。

    tier: core(核心，长期保留)/work(工作，短期)/archive(归档)
    实际调用 lse-engine delta_update，ID 加分层前缀。
    """
    if tier not in _LSE_TIERS:
        return {"ok": False, "error": f"未知层级: {tier}（可选 {_LSE_TIERS}）"}
    # 内容哈希 → 稳定 ID（同内容汇聚，防重复；枢纽优先的汇聚基础）
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    lesson_id = f"{_TIER_PREFIX[tier]}{h}"
    return delta_update_lesson(lesson_id, delta, threshold)


def auto_extract_lessons(text: str, tier: str = "work",
                         patterns: list[str] | None = None) -> dict:
    """自动教训提取（抄 mem0 自动记忆提取）：从工具结果/对话文本中提取教训。

    零 LLM 规则版：识别文本中的教训模式（"教训/注意/避免/必须/不要/经验"等信号），
    每条约 200 字符，自动入库（分层）。LLM 版可在后续接 kb_query/蒸馏模型。

    text:     源文本（工具结果/错误信息/对话）
    tier:     默认层级 work（短期），核心教训可显式传 core
    patterns: 自定义教训信号词（默认内置中英信号词）
    """
    if not text or not text.strip():
        return {"ok": False, "error": "text 为空"}
    default_signals = [
        "教训", "注意", "避免", "必须", "不要", "切记", "经验",
        "lesson", "avoid", "never", "always", "remember", "pitfall",
        "错误", "失败", "问题", "bug",
    ]
    signals = patterns or default_signals
    # 统一换行符后按行切分（跳过空/过短行）
    sentences = [s.strip() for s in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if len(s.strip()) >= 8]
    extracted = []
    # IDE 增强 291：教训语言标记（从源文本 file 路径后缀推断——
    # 多语言项目的教训可回溯语言来源）
    _lang = ""
    _m = re.search(r"([A-Za-z_][\w./\\-]*\.(?:rs|py|go|ts|tsx|js|jsx|gd|c|cpp|"
                   r"h|hpp|cs|lua|sh|bash|java|kt|kts|swift|php|rb|ps1|dart))",
                   text)
    if _m:
        _lang = os.path.splitext(_m.group(1))[1].lower().lstrip(".")
    for sent in sentences:
        if any(sig in sent for sig in signals):
            # 截断 200 字符（压缩学习：防状态文件膨胀）
            summary = sent if len(sent) <= 200 else sent[:197] + "..."
            if _lang:
                summary = f"[{_lang}] {summary}"  # 语言前缀——recall 一眼可溯
            r = lesson_store_tiered(tier, summary)
            if r.get("ok", False) and r.get("result", {}).get("ok", True):
                extracted.append(summary)
    return {"ok": True, "tier": tier, "extracted": len(extracted),
            "lessons": extracted[:20], "language": _lang,
            "note": "规则版自动提取（信号词匹配）；LLM 版可接蒸馏模型升级"}
