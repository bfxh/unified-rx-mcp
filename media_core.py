#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""media_core.py — 剪辑/动画检查核心（2026-08-17）。

用户要求（2026-08-17）：IDE 对剪辑和动画的提升；"看看有没有相关的工具 Rust 的，
如果没有就自己造一个"（→ rx-media crate）；渲染模拟用**完整渲染验证**。

- video_info(path)    视频容器信息：优先调 rx-media（Rust 零依赖），
                       无则纯 Python atom 解析降级——时长/分辨率/帧率/编码/损坏
- timeline_check(path) 剪辑时间线检查（Blender VSE 为主）：bpy 批处理读 .blend
                       序列编辑器——素材断链/时长越界/帧率混用/分辨率不一致
- anim_check(path)     动画检查：.blend 场景（action/关键帧/骨骼/驱动器）via bpy；
                       .glb 走 animations/skin JSON 解析（落地 PERCEPTION_PLAN_v3 glb_info）
- render_sim(path)     完整渲染验证：blender -b 批处理渲染（默认全帧，可配
                       帧范围/引擎/分辨率），验证输出文件齐全 + 无错误日志
- 降级链：Blender 不可用 → 静态检查 + 明确提示（graceful）
"""
import json
import os
import re
import struct
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RX_MEDIA = os.path.join(_HERE, "rx-media", "target", "release", "rx-media.exe")
# bpy 批处理脚本目录
_SCRIPTS = os.path.join(_HERE, "media_scripts")


def _blender_exe() -> str:
    """探测 Blender：环境变量 BLENDER_EXE > local-tools 注册表 > 常见路径。"""
    env = os.environ.get("BLENDER_EXE")
    if env and os.path.isfile(env):
        return env
    reg = os.path.join(os.path.expanduser("~"), ".unified-rx", "local-tools.json")
    try:
        data = json.load(open(reg, encoding="utf-8"))
        for t in data.get("tools", []):
            if t.get("name") == "blender" and os.path.isfile(t["path"]):
                return t["path"]
    except (OSError, ValueError):
        pass
    candidates = [r"D:\rj\GJ\Blender 5.2\blender.exe"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


# ── video_info：rx-media 优先 + Python 降级 ─────────────────────────────

def _py_atom_info(path: str) -> dict:
    """纯 Python MP4/MOV atom 解析（降级链；与 rx-media 同字段）。"""
    try:
        data = open(path, "rb").read()
    except OSError as e:
        return {"ok": False, "error": f"读取失败: {e}"}
    info = {"path": path, "file_size": len(data)}
    if len(data) < 12:
        info.update({"ok": False, "damaged": True, "reason": "文件过短（<12 字节）"})
        return info

    def walk(buf: bytes, off: int, end: int):
        boxes = []
        pos = off
        while pos + 8 <= end:
            size = struct.unpack(">I", buf[pos:pos + 4])[0]
            kind = buf[pos + 4:pos + 8]
            hlen = 8
            if size == 1 and pos + 16 <= end:
                size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
                hlen = 16
            elif size == 0:
                size = end - pos
            if size < hlen or pos + size > end:
                break
            boxes.append((kind, pos + hlen, pos + size))
            pos += size
        return boxes

    top = walk(data, 0, len(data))
    if not top:
        info.update({"ok": False, "damaged": True, "reason": "无有效 box"})
        return info
    # ftyp
    for kind, s, e in top:
        if kind == b"ftyp" and e - s >= 8:
            info["brand"] = data[s:s + 4].decode("latin1", "replace")
            info["compatible"] = [data[s + 8 + i * 4:s + 12 + i * 4].decode("latin1", "replace")
                                  for i in range((e - s - 8) // 4)]
            break
    moov = next(((s, e) for k, s, e in top if k == b"moov"), None)
    if moov is None:
        info.update({"ok": False, "damaged": True, "reason": "moov box 缺失（不可播放/损坏）"})
        return info
    ms, me = moov
    # mvhd
    for kind, s, e in walk(data, ms, me):
        if kind == b"mvhd":
            ver = data[s]
            # v0: ver/flags(4)+creation(4)+modification(4)+timescale(4)@12+duration(4)@16
            # v1: ...+creation(8)+modification(8)+timescale(4)@20+duration(8)@24
            # 长度守卫（审查 2026-08-17）：payload 不足直接跳过，防 struct.error
            if ver == 1 and e - s >= 32:
                ts = struct.unpack(">I", data[s + 20:s + 24])[0]
                dur = struct.unpack(">Q", data[s + 24:s + 32])[0]
            elif e - s >= 20:
                ts = struct.unpack(">I", data[s + 12:s + 16])[0]
                dur = struct.unpack(">I", data[s + 16:s + 20])[0]
            else:
                ts = 0
                dur = 0
            info["timescale"] = ts
            info["duration"] = dur
            info["duration_sec"] = round(dur / ts, 3) if ts else 0.0
            break
    # trak 列表
    tracks = []
    for kind, s, e in walk(data, ms, me):
        if kind != b"trak":
            continue
        t = {"kind": "", "codec": "", "width": 0, "height": 0, "samples": 0}
        for k2, s2, e2 in walk(data, s, e):
            if k2 == b"tkhd":
                ver = data[s2]
                tid_off = 20 if ver == 1 else 12
                if e2 - s2 >= tid_off + 4:
                    t["id"] = struct.unpack(">I", data[s2 + tid_off:s2 + tid_off + 4])[0]
                if e2 - s2 >= 12:
                    t["width"] = struct.unpack(">I", data[e2 - 8:e2 - 4])[0] >> 16
                    t["height"] = struct.unpack(">I", data[e2 - 4:e2])[0] >> 16
            if k2 == b"mdia":
                for k3, s3, e3 in walk(data, s2, e2):
                    if k3 == b"hdlr" and e3 - s3 >= 12:
                        t["kind"] = data[s3 + 8:s3 + 12].decode("latin1", "replace")
                    if k3 == b"minf":
                        for k4, s4, e4 in walk(data, s3, e3):
                            if k4 == b"stbl":
                                for k5, s5, e5 in walk(data, s4, e4):
                                    if k5 == b"stsd" and e5 - s5 >= 8:
                                        n = struct.unpack(">I", data[s5 + 4:s5 + 8])[0]
                                        for k6, s6, _ in walk(data, s5 + 8, e5)[:max(n, 1)]:
                                            t["codec"] = k6.decode("latin1", "replace")
                                            break
                                    if k5 == b"stts" and e5 - s5 >= 8:
                                        n = struct.unpack(">I", data[s5 + 4:s5 + 8])[0]
                                        total = 0
                                        for i in range(n):
                                            off = s5 + 8 + i * 8
                                            if off + 8 > e5:
                                                break
                                            total += struct.unpack(">I", data[off:off + 4])[0]
                                        t["samples"] = total
        tracks.append(t)
    info["tracks"] = tracks
    video = next((t for t in tracks if t.get("kind") == "vide"), None)
    audio = next((t for t in tracks if t.get("kind") == "soun"), None)
    if video:
        info.update({"width": video["width"], "height": video["height"],
                     "codec": video["codec"], "has_video": True})
    else:
        info["has_video"] = False
    info["has_audio"] = audio is not None
    info["track_count"] = len(tracks)
    dur = info.get("duration_sec", 0)
    if video and video["samples"] and dur:
        video["fps_est"] = round(video["samples"] / dur, 2)
    damaged = False
    reason = ""
    if info.get("duration") == 0 and tracks:
        damaged, reason = True, "duration=0（时长信息缺失/损坏）"
    if not tracks:
        damaged, reason = True, "无轨道（空文件或损坏）"
    info["damaged"] = damaged
    if damaged:
        info["reason"] = reason
    info["ok"] = not damaged
    return info


def video_info(path: str) -> dict:
    """视频容器信息：rx-media（Rust）优先，失败降级纯 Python atom 解析。"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if os.path.isfile(_RX_MEDIA):
        try:
            r = subprocess.run([_RX_MEDIA, "info", path], capture_output=True,
                               text=True, timeout=30, errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout.strip())
                d["engine"] = "rx-media(Rust)"
                return d
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    d = _py_atom_info(path)
    d["engine"] = "python-fallback"
    return d


# ── Blender 批处理（timeline/anim/render）──────────────────────────────

def _bpy_script(name: str) -> str:
    p = os.path.join(_SCRIPTS, name)
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


def _run_blender(blend: str, script: str, extra_args: list[str] | None = None,
                 timeout: int = 600) -> dict:
    """blender -b <blend> -P <script> -- <args>：返回 {ok, stdout, stderr, returncode}。"""
    blender = _blender_exe()
    if not blender:
        return {"ok": False, "error": "Blender 不可用（BLENDER_EXE 或 D:\\rj\\GJ\\Blender 5.2）"}
    script_path = os.path.join(_SCRIPTS, script)
    if not os.path.isfile(script_path):
        return {"ok": False, "error": f"bpy 脚本缺失: {script_path}"}
    cmd = [blender, "-b"]
    if blend and os.path.isfile(blend):
        cmd.append(blend)
    cmd += ["-P", script_path, "--", *(extra_args or [])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return {"ok": True, "returncode": r.returncode,
                "stdout": (r.stdout or "")[-20000:], "stderr": (r.stderr or "")[-5000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Blender 超时（>{timeout}s）"}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def timeline_check(path: str) -> dict:
    """剪辑时间线检查（Blender VSE 为主）：素材断链/时长越界/帧率混用/分辨率不一致。"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not path.lower().endswith(".blend"):
        return {"ok": False, "error": "仅支持 .blend（Blender VSE 时间线）",
                "hint": "视频素材检查用 video_info；时间线请导出 .blend"}
    r = _run_blender(path, "vse_check.py", timeout=600)
    if not r["ok"]:
        return {"ok": False, "error": r["error"],
                "hint": "Blender 不可用——降级：请用 video_info 逐素材检查"}
    # 解析 bpy 脚本输出的 JSON（在 stdout 尾部）
    out = r["stdout"]
    m = re.search(r"__MEDIA_JSON__\s*(\{.*\})", out, re.S)
    if not m:
        return {"ok": False, "error": "bpy 脚本未输出结果", "stderr": r["stderr"][-500:],
                "returncode": r["returncode"]}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"ok": False, "error": "bpy 输出 JSON 解析失败", "raw": m.group(1)[:300]}
    d["engine"] = "blender-vse"
    return d


def anim_check(path: str) -> dict:
    """动画检查：.blend 场景（action/关键帧/骨骼/驱动器）via bpy；.glb animations 解析。"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    low = path.lower()
    if low.endswith(".glb") or low.endswith(".gltf"):
        return _glb_anim_check(path)
    if not low.endswith(".blend"):
        return {"ok": False, "error": "仅支持 .blend（Blender 动画）或 .glb/.gltf"}
    r = _run_blender(path, "anim_check.py", timeout=600)
    if not r["ok"]:
        return {"ok": False, "error": r["error"],
                "hint": "Blender 不可用——降级：.blend 无法静态解析，请装 Blender 后重试"}
    out = r["stdout"]
    m = re.search(r"__MEDIA_JSON__\s*(\{.*\})", out, re.S)
    if not m:
        return {"ok": False, "error": "bpy 脚本未输出结果", "stderr": r["stderr"][-500:],
                "returncode": r["returncode"]}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"ok": False, "error": "bpy 输出 JSON 解析失败", "raw": m.group(1)[:300]}
    d["engine"] = "blender-anim"
    return d


def _glb_anim_check(path: str) -> dict:
    """GLB/GLTF 动画检查：animations（关键帧/通道）与 skin（骨骼）完整性。"""
    try:
        data = open(path, "rb").read()
    except OSError as e:
        return {"ok": False, "error": f"读取失败: {e}"}
    low = path.lower()
    if low.endswith(".gltf"):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            return {"ok": False, "error": f"gltf JSON 解析失败: {e}"}
    else:
        if len(data) < 12 or data[:4] != b"glTF":
            return {"ok": False, "damaged": True, "reason": "非 GLB 魔数"}
        try:
            jlen = struct.unpack(">I", data[12:16])[0]
            doc = json.loads(data[20:20 + jlen].decode("utf-8"))
        except (struct.error, UnicodeDecodeError, ValueError) as e:
            return {"ok": False, "error": f"GLB JSON chunk 解析失败: {e}"}
    # 顶层结构校验（审查 2026-08-17）：非 dict JSON 或元素非 dict 直接拒绝，
    # 防 AttributeError 崩溃
    if not isinstance(doc, dict):
        return {"ok": False, "damaged": True, "reason": "GLB JSON 顶层非对象（损坏）"}
    anims = doc.get("animations") or []
    skins = doc.get("skins") or []
    meshes = doc.get("meshes") or []
    nodes = doc.get("nodes") or []
    if not all(isinstance(a, dict) for a in anims) or \
            not all(isinstance(s, dict) for s in skins):
        return {"ok": False, "damaged": True, "reason": "GLB animations/skins 元素非对象（损坏）"}
    issues = []
    # 动画完整性：channels/samplers 引用越界（channels 元素须为 dict）
    for i, a in enumerate(anims):
        channels = [c for c in a.get("channels", []) if isinstance(c, dict)]
        if len(channels) != len(a.get("channels", [])):
            issues.append(f"动画[{i}] 存在非对象 channel（损坏）")
        n_ch = len(channels)
        n_sa = len(a.get("samplers", []))
        if n_ch == 0:
            issues.append(f"动画[{i}] '{a.get('name', '?')}' 无通道（空动画）")
        if n_sa == 0:
            issues.append(f"动画[{i}] 无 sampler")
        for ch in channels:
            # target 嵌套类型校验（复审 2026-08-17 warn 项）：target 非 dict
            # 时 tgt.get 抛 AttributeError——损坏输入不崩溃
            tgt = ch.get("target")
            if not isinstance(tgt, dict):
                issues.append(f"动画[{i}] channel target 非对象（损坏）")
                continue
            node = tgt.get("node")
            if node is not None and node >= len(nodes):
                issues.append(f"动画[{i}] 通道 target.node={node} 越界（nodes 共 {len(nodes)}）")
    # 骨骼完整性：skin joints 引用（joints 非 list 视为损坏）
    for i, s in enumerate(skins):
        joints = s.get("joints")
        if joints is not None and not isinstance(joints, list):
            issues.append(f"skin[{i}] joints 非列表（损坏）")
            joints = []
        joints = joints or []
        bad = [j for j in joints if j >= len(nodes)]
        if bad:
            issues.append(f"skin[{i}] joints 越界: {bad}")
        if not joints:
            issues.append(f"skin[{i}] 无 joints（骨骼缺失）")
    # 蒙皮引用：mesh 有 skin 但场景无 skin 节点
    return {
        "ok": not issues, "path": path, "engine": "glb-parser",
        "animations": len(anims), "skins": len(skins),
        "meshes": len(meshes), "nodes": len(nodes),
        "anim_names": [a.get("name", f"anim_{i}") for i, a in enumerate(anims)],
        "issues": issues, "damaged": bool(issues),
        "advice": ("动画/骨骼完整" if not issues else "；".join(issues[:8])),
    }


def render_sim(path: str, frames: str = "ALL", engine: str = "CYCLES",
               resolution: int = 0, timeout: int = 1800) -> dict:
    """完整渲染验证（用户选定）：blender -b 批处理渲染，验证输出齐全 + 无错误。"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not path.lower().endswith(".blend"):
        return {"ok": False, "error": "仅支持 .blend 场景渲染"}
    args = [frames, engine, str(resolution)]
    r = _run_blender(path, "render_sim.py", extra_args=args, timeout=timeout)
    if not r["ok"]:
        return {"ok": False, "error": r["error"],
                "hint": "Blender 不可用——降级：无法渲染验证，请装 Blender 后重试"}
    out = r["stdout"]
    m = re.search(r"__MEDIA_JSON__\s*(\{.*\})", out, re.S)
    if not m:
        return {"ok": False, "error": "bpy 脚本未输出结果", "stderr": r["stderr"][-500:],
                "returncode": r["returncode"]}
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"ok": False, "error": "bpy 输出 JSON 解析失败", "raw": m.group(1)[:300]}
    d["engine"] = "blender-render"
    return d


if __name__ == "__main__":
    import json as _json
    if len(sys.argv) > 2:
        action, path = sys.argv[1], sys.argv[2]
        if action == "video":
            print(_json.dumps(video_info(path), ensure_ascii=False, indent=2))
        elif action == "timeline":
            print(_json.dumps(timeline_check(path), ensure_ascii=False, indent=2))
        elif action == "anim":
            print(_json.dumps(anim_check(path), ensure_ascii=False, indent=2))
        elif action == "render":
            print(_json.dumps(render_sim(path), ensure_ascii=False, indent=2))
