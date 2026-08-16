#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""net_core — 弱网模拟桥接（Python → rx-net Rust 子进程，按需启停）。

对齐 search_core/telemetry_core 模式：找 release/debug exe，Popen 启停。
区别：rx-net 不是常驻服务而是代理进程——启动后转发连接并注入混沌，
stdin 写 "stop" 优雅退出（main.rs 内置）。多代理并存（不同 listen 端口）。

- start(listen, target, ...) 启动混沌代理（listen 为空/0 自动分配端口）
- stop(listen=None)       停止指定或全部
- status()                运行中代理清单
- sanity(...)             一次性自检（--sanity echo 往返）
失败静默（未编译 → 调用方降级）；环境变量 RX_NET=0 禁用。
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time

_NET_EXE = None
for _cand in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-net", "target", "release", "rx-net.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-net", "target", "debug", "rx-net.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-net", "target", "release", "rx-net"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-net", "target", "debug", "rx-net"),
    # 中文路径仓库固定 target-dir（.cargo/config.toml）——重编译后产物在 D:/rj/.rx-target
    os.path.join("D:/rj/.rx-target", "release", "rx-net.exe"),
    os.path.join("D:/rj/.rx-target", "debug", "rx-net.exe"),
):
    if os.path.exists(_cand):
        _NET_EXE = _cand
        break

_procs: dict[str, dict] = {}  # listen -> {proc, target, cfg, started}
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("RX_NET", "1") != "0" and _NET_EXE is not None


def free_port(host: str = "127.0.0.1") -> int:
    """分配空闲端口（bind 0 探测后释放，调用方立即使用——本地工具可接受竞态）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _cfg_dict(delay: float = 0, loss: float = 0,
              reorder: float = 0, bandwidth: int = 0) -> dict:
    """参数规整 + 校验（返回 (cfg, err) 二选一模式由调用方处理）。"""
    delay = max(0.0, float(delay))
    loss = min(100.0, max(0.0, float(loss)))
    reorder = min(100.0, max(0.0, float(reorder)))
    bandwidth = max(0, int(bandwidth))
    return {"delay_ms": delay, "loss_pct": loss,
            "reorder_pct": reorder, "bandwidth_kbps": bandwidth}


def start(listen: str = "", target: str = "127.0.0.1:80",
          delay: float = 0, loss: float = 0,
          reorder: float = 0, bandwidth: int = 0) -> dict | None:
    """启动混沌代理。listen 为空/端口 0 → 自动分配。返回状态 dict。"""
    if not enabled():
        return None
    listen = (listen or "").strip()
    if not listen or listen.endswith(":0"):
        listen = f"127.0.0.1:{free_port()}"
    cfg = _cfg_dict(delay, loss, reorder, bandwidth)
    with _lock:
        if listen in _procs and _procs[listen]["proc"].poll() is None:
            p = _procs[listen]
            return {"ok": True, "listen": listen, "target": p["target"],
                    "cfg": p["cfg"], "pid": p["proc"].pid,
                    "already_running": True}
        try:
            proc = subprocess.Popen(
                [_NET_EXE, "--listen", listen, "--target", target,
                 "--delay", str(int(cfg["delay_ms"])),
                 "--loss", f'{cfg["loss_pct"]:.6f}',
                 "--reorder", f'{cfg["reorder_pct"]:.6f}',
                 "--bandwidth", str(cfg["bandwidth_kbps"])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", bufsize=1)
        except OSError:
            return None
        # 等待代理真正就绪（listener bind 成功；启动失败立刻退出）
        time.sleep(0.05)
        if proc.poll() is not None:
            return {"ok": False, "listen": listen, "error": "rx-net 启动即退出"}
        _procs[listen] = {"proc": proc, "target": target,
                          "cfg": cfg, "started": time.time()}
        return {"ok": True, "listen": listen, "target": target,
                "cfg": cfg, "pid": proc.pid}


def _stop_one(rec: dict) -> None:
    proc = rec["proc"]
    if proc.poll() is None:
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001 —— 超时强杀
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def stop(listen: str = "") -> dict:
    """停止指定（listen 非空）或全部代理。"""
    if not enabled():
        return {"ok": False, "error": "rx-net 不可用（未编译或 RX_NET=0）"}
    stopped: list[str] = []
    with _lock:
        keys = [listen] if listen else list(_procs.keys())
        for k in keys:
            rec = _procs.pop(k, None)
            if rec is not None:
                _stop_one(rec)
                stopped.append(k)
    return {"ok": True, "stopped": stopped}


def status() -> dict:
    """运行中代理清单（清理已退出的僵尸记录）。"""
    if not enabled():
        return {"ok": False, "error": "rx-net 不可用（未编译或 RX_NET=0）"}
    out: list[dict] = []
    with _lock:
        dead = []
        for k, rec in _procs.items():
            if rec["proc"].poll() is not None:
                dead.append(k)
                continue
            out.append({"listen": k, "target": rec["target"],
                        "cfg": rec["cfg"], "pid": rec["proc"].pid,
                        "uptime_s": round(time.time() - rec["started"], 1)})
        for k in dead:
            del _procs[k]
    return {"ok": True, "proxies": out, "count": len(out)}


def sanity(delay: float = 0, loss: float = 0,
           reorder: float = 0, bandwidth: int = 0) -> dict | None:
    """一次性自检（echo 往返 + 混沌注入验证）。"""
    if not enabled():
        return None
    cfg = _cfg_dict(delay, loss, reorder, bandwidth)
    try:
        r = subprocess.run(
            [_NET_EXE, "--sanity", "--delay", str(int(cfg["delay_ms"])),
             "--loss", f'{cfg["loss_pct"]:.6f}',
             "--reorder", f'{cfg["reorder_pct"]:.6f}',
             "--bandwidth", str(cfg["bandwidth_kbps"])],
            capture_output=True, text=True, encoding="utf-8",
            timeout=30.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}
    return {"ok": True, "result": r.stdout.strip()}


def shutdown() -> None:
    stop()
