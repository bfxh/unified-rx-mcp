#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_media_core.py — 剪辑/动画检查核心测试（rx-media parity/降级链/GLB/Blender）。"""
import json
import os
import shutil
import struct
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import media_core as mc  # noqa: E402


def _make_mp4(path: str) -> str:
    """构造最小合法 MP4（ftyp + moov{mvhd, trak{tkhd, mdia{mdhd, hdlr, minf{stbl{stsd, stts}}}}}）。"""
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), kind) + payload

    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isommp42")
    mvhd = box(b"mvhd", struct.pack(">IIIIII", 0, 0, 0, 1000, 5000, 0) + b"\x00" * 80)
    tkhd = box(b"tkhd", (struct.pack(">I", 0) + struct.pack(">II", 0, 0) +
                          struct.pack(">I", 1) + struct.pack(">I", 0) +
                          struct.pack(">I", 5000) + b"\x00" * 8 +
                          struct.pack(">HHHH", 0, 0, 0, 0) + b"\x00" * 36 +
                          struct.pack(">II", 1920 << 16, 1080 << 16)))
    mdhd = box(b"mdhd", struct.pack(">IIIIIIH", 0, 0, 0, 1000, 5000, 0x55C4, 0))
    hdlr = box(b"hdlr", struct.pack(">II4s", 0, 0, b"vide") + b"\x00" * 12 + b"VH\x00")
    stsd = box(b"stsd", struct.pack(">II", 0, 1) + box(b"avc1", b"\x00" * 78))
    stts = box(b"stts", struct.pack(">III", 0, 1, 250) + struct.pack(">I", 20))
    stbl = box(b"stbl", stsd + stts)
    mdia = box(b"mdia", mdhd + hdlr + box(b"minf", stbl))
    moov = box(b"moov", mvhd + box(b"trak", tkhd + mdia))
    with open(path, "wb") as f:
        f.write(ftyp + moov)
    return path


def _make_glb(path: str, doc: dict) -> str:
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(body) % 4) % 4
    body += b" " * pad
    total = 12 + 8 + len(body)
    with open(path, "wb") as f:
        f.write(b"glTF" + struct.pack(">II", 2, total))
        f.write(struct.pack(">I4s", len(body), b"JSON"))
        f.write(body)
    return path


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp(prefix="media_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── video_info ─────────────────────────────────────────────────────────

def test_video_info_rust_engine(tmp):
    p = _make_mp4(os.path.join(tmp, "t.mp4"))
    r = mc.video_info(p)
    assert r["ok"] is True
    assert r["engine"] == "rx-media(Rust)"
    assert (r["width"], r["height"]) == (1920, 1080)
    assert r["duration_sec"] == 5.0
    assert r["codec"] == "avc1"
    assert r["has_video"] is True and r["has_audio"] is False
    assert r["damaged"] is False


def test_video_info_python_fallback_parity(tmp):
    """Python 降级解析与 Rust 同字段（parity）。"""
    p = _make_mp4(os.path.join(tmp, "t.mp4"))
    rust = mc.video_info(p)
    py = mc._py_atom_info(p)
    for key in ("width", "height", "duration_sec", "codec", "has_video", "damaged"):
        assert rust[key] == py[key], f"{key}: rust={rust[key]} py={py[key]}"


def test_video_info_damaged(tmp):
    bad = os.path.join(tmp, "junk.mp4")
    with open(bad, "wb") as f:
        f.write(b"notamp4file")
    r = mc.video_info(bad)
    assert r["ok"] is False and r["damaged"] is True
    # 截断（无 moov）
    p = _make_mp4(os.path.join(tmp, "full.mp4"))
    trunc = os.path.join(tmp, "trunc.mp4")
    with open(p, "rb") as f:
        data = f.read()
    with open(trunc, "wb") as f:
        f.write(data[:60])
    r2 = mc.video_info(trunc)
    assert r2["ok"] is False and "moov" in r2["reason"]


def test_video_info_missing(tmp):
    r = mc.video_info(os.path.join(tmp, "nope.mp4"))
    assert r["ok"] is False


# ── GLB 动画检查 ───────────────────────────────────────────────────────

def test_glb_anim_ok(tmp):
    doc = {
        "asset": {"version": "2.0"},
        "nodes": [{"name": "root"}, {"name": "bone_1"}],
        "animations": [{"name": "walk", "channels": [
            {"sampler": 0, "target": {"node": 1, "path": "translation"}}],
            "samplers": [{"input": 0, "output": 1}]}],
        "skins": [{"joints": [1]}],
    }
    p = _make_glb(os.path.join(tmp, "a.glb"), doc)
    r = mc.anim_check(p)
    assert r["ok"] is True
    assert r["animations"] == 1 and r["skins"] == 1
    assert r["issues"] == []


def test_glb_anim_broken_refs(tmp):
    doc = {
        "asset": {"version": "2.0"},
        "nodes": [{"name": "root"}],
        "animations": [{"name": "bad", "channels": [
            {"sampler": 0, "target": {"node": 99, "path": "rotation"}}],
            "samplers": [{"input": 0, "output": 1}]}],
        "skins": [{"joints": [5]}],
    }
    p = _make_glb(os.path.join(tmp, "b.glb"), doc)
    r = mc.anim_check(p)
    assert r["ok"] is False
    assert any("越界" in i for i in r["issues"])


def test_glb_bad_magic(tmp):
    p = os.path.join(tmp, "c.glb")
    with open(p, "wb") as f:
        f.write(b"NOTGLB" + b"\x00" * 20)
    r = mc.anim_check(p)
    assert r["ok"] is False and r.get("damaged") is True


# ── Blender 批处理（可用则真实跑，否则跳过）─────────────────────────────

def _blender_available() -> bool:
    return bool(mc._blender_exe())


@pytest.mark.skipif(not _blender_available(), reason="Blender 不可用")
def test_blender_vse_check_empty(tmp):
    """空场景 VSE 检查（Blender 真实批处理）。"""
    r = mc.timeline_check(os.path.join(tmp, "nope.blend"))
    # 文件不存在 → error
    assert r["ok"] is False
    # 真实空场景：blender -b -P vse_check.py（无文件参数 → 默认空场景）
    res = mc._run_blender("", "vse_check.py", timeout=300)
    assert res["ok"] is True
    import re as _re
    m = _re.search(r"__MEDIA_JSON__\s*(\{.*\})", res["stdout"], _re.S)
    assert m, res["stderr"][-300:]
    d = json.loads(m.group(1))
    assert d["ok"] is True


def test_timeline_check_non_blend(tmp):
    p = os.path.join(tmp, "x.mp4")
    open(p, "wb").write(b"x" * 100)
    r = mc.timeline_check(p)
    assert r["ok"] is False
    assert "仅支持 .blend" in r["error"]


# ── MCP 工具集成 ────────────────────────────────────────────────────────

def test_media_check_tool_video(tmp):
    """media_check MCP 工具：video action 集成（注册/分发/JSON 输出）。"""
    import server
    p = _make_mp4(os.path.join(tmp, "t.mp4"))
    r = server._call("media_check", {"action": "video", "path": p})
    assert r[0].text
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert d["width"] == 1920 and d["height"] == 1080


def test_media_check_tool_render_no_blend(tmp):
    """media_check render 对非 .blend graceful 拒绝。"""
    import server
    p = os.path.join(tmp, "x.mp4")
    open(p, "wb").write(b"x" * 100)
    r = server._call("media_check", {"action": "render", "path": p})
    d = json.loads(r[0].text)
    assert d["ok"] is False
    assert "仅支持 .blend" in d["error"]


def test_media_check_tool_bad_action(tmp):
    import server
    r = server._call("media_check", {"action": "nope", "path": tmp})
    assert "Error" in r[0].text


# ── 审查回归（2026-08-17）：损坏输入不崩溃 ───────────────────────────────

def test_video_info_ext64_overflow_no_crash(tmp):
    """64 位扩展 size 接近 u64::MAX：Rust 回绕修复——不 panic、不崩溃。"""
    p = os.path.join(tmp, "ext64.mp4")
    with open(p, "wb") as f:
        f.write(struct.pack(">I4sQ", 1, b"free", 0xFFFF_FFFF_FFFF_FFFB))
        f.write(b"\x00" * 16)
    # 不抛异常（Rust 路径或 Python 降级均安全）
    r = mc.video_info(p)
    assert isinstance(r, dict)


def test_video_info_short_mvhd_no_crash(tmp):
    """mvhd payload 不足（12 字节）：Python 降级长度守卫——不抛 struct.error。"""
    def box(kind, payload):
        return struct.pack(">I4s", 8 + len(payload), kind) + payload
    data = (box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isom") +
            box(b"moov", box(b"mvhd", b"\x00" * 12)))
    p = os.path.join(tmp, "short_mvhd.mp4")
    with open(p, "wb") as f:
        f.write(data)
    d = mc._py_atom_info(p)  # 不抛异常
    assert d["duration_sec"] == 0.0


def test_glb_non_object_json_no_crash(tmp):
    """GLB JSON 顶层非对象（数组）：结构校验——返回 damaged 不抛 AttributeError。"""
    body = b'[1,2,3]'
    pad = (4 - len(body) % 4) % 4
    body += b" " * pad
    p = os.path.join(tmp, "arr.glb")
    with open(p, "wb") as f:
        f.write(b"glTF" + struct.pack(">II", 2, 12 + 8 + len(body)))
        f.write(struct.pack(">I4s", len(body), b"JSON"))
        f.write(body)
    r = mc.anim_check(p)
    assert r["ok"] is False
    assert "非对象" in r.get("reason", "")


def test_glb_bad_channels_no_crash(tmp):
    """GLB animations 元素 dict 但 channels 元素非 dict：不抛 AttributeError。"""
    doc = {"asset": {"version": "2.0"}, "nodes": [{"name": "n"}],
           "animations": [{"name": "bad", "channels": ["not-a-dict"],
                           "samplers": []}]}
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(body) % 4) % 4
    body += b" " * pad
    p = os.path.join(tmp, "chan.glb")
    with open(p, "wb") as f:
        f.write(b"glTF" + struct.pack(">II", 2, 12 + 8 + len(body)))
        f.write(struct.pack(">I4s", len(body), b"JSON"))
        f.write(body)
    r = mc.anim_check(p)
    assert "非对象 channel" in " ".join(r.get("issues", []))


def test_glb_bad_target_and_joints_no_crash(tmp):
    """channel target 非 dict / skin joints 非 list：嵌套类型校验——不崩溃（复审 warn 项）。"""
    doc = {"asset": {"version": "2.0"}, "nodes": [{"name": "n"}],
           "animations": [{"name": "a", "channels": [{"sampler": 0, "target": "oops"}],
                           "samplers": [{"input": 0, "output": 1}]}],
           "skins": [{"joints": 42}]}
    body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(body) % 4) % 4
    body += b" " * pad
    p = os.path.join(tmp, "nested.glb")
    with open(p, "wb") as f:
        f.write(b"glTF" + struct.pack(">II", 2, 12 + 8 + len(body)))
        f.write(struct.pack(">I4s", len(body), b"JSON"))
        f.write(body)
    r = mc.anim_check(p)  # 不抛异常
    joined = " ".join(r.get("issues", []))
    assert "target 非对象" in joined
    assert "joints 非列表" in joined
