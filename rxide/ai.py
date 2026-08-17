#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/ai.py — LLM 对话（仅 stdlib urllib；支持 SSE 流式逐块）。"""
import json
import urllib.request

from . import settings


def parse_sse_line(line: str) -> dict | None:
    """解析单行 SSE：只认 `data: {...}` JSON；忽略 [DONE]/空行/非法 JSON。"""
    s = (line or "").strip()
    if not s.startswith("data:"):
        return None
    payload = s[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _request(cfg: dict, messages: list, stream: bool, timeout: int):
    """POST {base_url}/chat/completions（失败抛异常，调用方捕获降级）。"""
    url = str(cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg.get("model", ""), "messages": messages,
                       "stream": stream}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key', '')}"})
    return urllib.request.urlopen(req, timeout=timeout)


def chat(messages: list[dict], cfg: dict | None = None, stream: bool = False,
         timeout: int = 120):
    """LLM 对话。cfg=None 自动 settings.load()。

    stream=False → {"ok": True, "content"} 或 {"ok": False, "error"}；
    stream=True  → 生成器：逐块 {"type": "token", "text"}，
                   结束 {"type": "done", "content"}，出错 {"type": "error", "error"}。
    """
    if cfg is None:
        cfg = settings.load()
    if not str(cfg.get("api_key") or "").strip():
        err = "未配置 API Key（右上角齿轮设置）"
        if not stream:
            return {"ok": False, "error": err}

        def _no_key():
            yield {"type": "error", "error": err}
        return _no_key()
    if not stream:
        try:
            with _request(cfg, messages, False, timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"].get("content") or ""
            return {"ok": True, "content": content}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _stream():
        acc = []
        try:
            with _request(cfg, messages, True, timeout) as resp:
                for raw in resp:  # 按行读 SSE
                    obj = parse_sse_line(raw.decode("utf-8", "replace"))
                    if not obj:
                        continue
                    try:
                        delta = obj["choices"][0].get("delta", {}).get("content")
                    except (KeyError, IndexError, AttributeError, TypeError):
                        delta = None
                    if delta:
                        acc.append(delta)
                        yield {"type": "token", "text": delta}
            yield {"type": "done", "content": "".join(acc)}
        except Exception as e:
            yield {"type": "error", "error": str(e)}
    return _stream()
