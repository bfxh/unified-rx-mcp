#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/settings.py — RX-IDE Lite 配置（~/.unified-rx/rxide.json，纯 stdlib）。

约定（对齐存量）：导入不创建目录——save() 按需创建。
"""
import json
import os

DATA_FILE = os.path.join(os.path.expanduser("~"), ".unified-rx", "rxide.json")

DEFAULTS = {"api_key": "", "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-reasoner", "font_size": 13, "theme": "dark",
            "preview_target": "http://127.0.0.1:17300"}


def load() -> dict:
    """读配置并与 DEFAULTS 合并（缺失键补默认；文件损坏返回 DEFAULTS）。"""
    cfg = dict(DEFAULTS)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # 文件不存在/损坏——默认配置兜底
    return cfg


def save(patch: dict) -> dict:
    """合并写回（只接受 DEFAULTS 的键），原子落盘，返回合并后配置。

    掩码保护：api_key 以 **** 开头视为前端掩码回写——忽略该键，
    防覆盖真实 Key（服务端双保险）。
    原子写：先写临时文件再 os.replace（Windows 亦原子）——
    ThreadingHTTPServer 并发保存不撕裂。
    """
    cfg = load()
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            continue
        if k == "api_key" and str(v or "").startswith("****"):
            continue  # 掩码回写忽略
        cfg[k] = v
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)  # 原子替换（临时文件随之消失）
    except OSError:
        pass  # 落盘失败静默（配置尽力而为）
    return cfg


def masked(cfg: dict) -> dict:
    """拷贝并掩码 api_key（****+后4位；不足4位全 ****；空则空串）。"""
    out = dict(cfg or {})
    key = str(out.get("api_key") or "")
    out["api_key"] = "" if not key else ("****" if len(key) < 4 else "****" + key[-4:])
    return out
