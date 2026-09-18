# -*- coding: utf-8 -*-
"""sys 域（S148）：混合架构调度观测与引导——P/E 核拓扑、线程视图、steer、设备。

背景（用户简报）：传统引擎默认所有核心性能一致 → 关键渲染线程被误调度到 E 核
导致帧率波动。本域把"识别 P/E 核 → 定位线程 → 按档位引导"整条链路工具化，
全部经 Rust 引擎 `rx-sys.exe`（零依赖手写 FFI；判定与实测见 skills/sys.md）。

四工具：
- sys_topology：CPU 厂商/型号、P/E/LP-E 分级（EfficiencyClass，双 API 交叉）、
  SMT、L3、NUMA、CPU 集清单；非混合平台如实报 uniform（不瞎标 P/E）。
- sys_threads：某进程每线程的 TID/名字/优先级/理想核/CPU 集。
- sys_steer：**执行·写**（requires_auth）——按档位引导线程：
    render（关键线程→P 核）：松开上一档硬掩码 → 限 P CPU 集 → 理想核指 P →
    AboveNormal 优先级 → 关 EcoQoS；`hard=true` 时再上硬亲和掩码（代价见文档）。
    background（后台→E 核）：同理反向（E 集 + BelowNormal + EcoQoS 开）。
- sys_devices：显示适配器清单（NVIDIA/AMD/Intel 识别，虚拟显示口去重）。

边界（如实）：Thread Director 无直接 API（我们给调度器更准的输入）；APO/iBOT
是厂商侧运行时不可编程；ITT/VTune 需厂商 SDK（本域只产出结构化 JSON）。
"""
import json
import os
import subprocess

from registry import tool
from tools.appaudit import _rs_exe          # exe 定位单一来源（S137 去重纪律）

_RX_SYS_EXE_NAME = "rx-sys.exe"


def _rx_sys_exe():
    return _rs_exe(_RX_SYS_EXE_NAME)


def _rx_sys_call(argv_tail, timeout=60):
    """薄壳转调 rx-sys.exe，返回结果 dict；错误语义与 appaudit 同款。

    退出码契约：0 = 工具级结果（含 {"error": ...} 包络，原样透传）；
    2 = 用法错误（转 ValueError）；其它 = 执行失败（带末行细节）。
    """
    exe = _rx_sys_exe()
    if not exe:
        raise ValueError("rx-sys.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指到已构建产物）")
    cp = subprocess.run([exe, *argv_tail], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout,
                        shell=False)
    lines = [ln for ln in (cp.stdout or "").strip().splitlines() if ln.strip()]
    if cp.returncode == 2:
        raise ValueError(f"rx-sys 用法错误: {(cp.stderr or '').strip()[:200]}")
    if not lines:
        raise ValueError(f"rx-sys 无输出（exit={cp.returncode}）: "
                         f"{(cp.stderr or '')[-200:]}")
    try:
        out = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ValueError(f"rx-sys 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode != 0:
        raise ValueError(f"rx-sys 执行失败（exit={cp.returncode}）: {lines[-1][:200]}")
    return out


@tool("sys_topology",
      "CPU 拓扑：厂商/型号、P/E/LP-E 核分级（EfficiencyClass 双 API 交叉）、SMT、L3、NUMA、CPU 集；非混合平台如实报 uniform",
      "sys",
      {"type": "object", "properties": {}, "required": []})
def sys_topology():
    return _rx_sys_call(["topology"])


@tool("sys_threads", "某进程每线程的 TID/名字/优先级/理想核/CPU 集（混合架构调度观测）",
      "sys",
      {"type": "object",
       "properties": {"pid": {"type": "integer", "description": "目标进程 PID"}},
       "required": ["pid"]})
def sys_threads(pid):
    p = int(pid)
    if p <= 0:
        raise ValueError("pid 必须为正整数")
    return _rx_sys_call(["threads", str(p)])


@tool("sys_steer",
      "按档位引导线程调度（写操作，需授权）：render=关键线程→P 核（松硬掩码/限 P 集/理想核 P/AboveNormal/关 EcoQoS）；background=后台→E 核（E 集/BelowNormal/EcoQoS 开）；hard=true 追加硬亲和（Intel 劝阻，代价见 skills/sys.md）",
      "sys",
      {"type": "object",
       "properties": {
           "pid": {"type": "integer", "description": "目标进程 PID"},
           "profile": {"type": "string", "enum": ["render", "background"],
                       "description": "render=关键线程→P 核；background=后台→E 核"},
           "tids": {"type": "array", "items": {"type": "integer"},
                    "description": "目标线程 TID 列表（缺省=该进程全部线程）"},
           "hard": {"type": "boolean",
                    "description": "追加硬亲和掩码（默认 false；Intel 明确劝阻）"},
       },
       "required": ["pid", "profile"]},
      requires_auth=True)
def sys_steer(pid, profile, tids=None, hard=False):
    p = int(pid)
    if p <= 0:
        raise ValueError("pid 必须为正整数")
    if profile not in ("render", "background"):
        raise ValueError("profile 必须是 render 或 background")
    tail = ["steer", str(p), profile]
    if hard:
        tail.append("--hard")
    for t in (tids or []):
        ti = int(t)
        if ti <= 0:
            raise ValueError(f"tid 必须为正整数: {t!r}")
        tail.append(str(ti))
    return _rx_sys_call(tail, timeout=120)


@tool("sys_devices", "显示适配器清单（NVIDIA/AMD/Intel 识别；同类显示口去重）", "sys",
      {"type": "object", "properties": {}, "required": []})
def sys_devices():
    return _rx_sys_call(["devices"])
