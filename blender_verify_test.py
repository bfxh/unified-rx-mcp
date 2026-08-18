# -*- coding: utf-8 -*-
"""blender_verify 自测（pytest——实地验证链不能坏，每次搞完自动看的基准）。"""
import os

import blender_verify as bv


def test_module_importable():
    assert bv.OUT_DEFAULT.endswith("blender_verify.png")


def test_analyze_toolbar_synthetic():
    """合成图：底部有白色图标 → BOTTOM_ICON 判定逻辑"""
    from PIL import Image
    img = Image.new("L", (64, 400), 40)  # 深色工具栏
    # 底部画白色块（模拟图标）
    for y in range(360, 390):
        for x in range(10, 40):
            img.putpixel((x, y), 255)
    info = bv.analyze_toolbar(img)
    assert info["segments"][7] > 0, "底部段应有白色像素"
    bottom = info["segments"][6] + info["segments"][7]
    assert bottom > 20


def test_analyze_toolbar_empty_bottom():
    """空底部 → 判定 NO"""
    from PIL import Image
    img = Image.new("L", (64, 400), 40)
    info = bv.analyze_toolbar(img)
    bottom = info["segments"][6] + info["segments"][7]
    assert bottom == 0


def test_ocr_file_tolerates_down_server(monkeypatch):
    """Umi-OCR 不可达时返回明确的失败行（monkeypatch——不依赖服务状态）"""
    def _raise(*a, **k):
        raise OSError("connection refused")
    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", _raise)
    lines = bv.ocr_file(bv.OUT_DEFAULT)
    assert isinstance(lines, list) and lines
    assert "OCR 失败" in lines[0]


def test_encoding_contract_no_mojibake(monkeypatch):
    """M1 集成契约（不依赖真实 Blender 窗口）：子进程输出中文
    （含 GBK 场景）→ 父进程 JSON 返回无替换符乱码"""
    import json
    import subprocess as sp
    import server

    class FakeResult:
        returncode = 0
        stdout = "WINDOW: Blender 测试 rect=(0,0,100,100)\nTOOLBAR: 段分布正常\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        # 验证编码契约被正确传递（env 含 PYTHONUTF8）
        env = kwargs.get("env", {})
        assert env.get("PYTHONUTF8") == "1", "子进程未强制 UTF-8"
        assert kwargs.get("encoding") == "utf-8"
        return FakeResult()

    monkeypatch.setattr(sp, "run", _fake_run)
    r = server._tool_blender_verify({"ocr": False})
    data = json.loads(r[0].text)
    assert data["ok"] is True and data["returncode"] == 0
    assert "\ufffd" not in data["stdout"], "中文被替换符损坏（M1 修复失效）"
    assert "Blender 测试" in data["stdout"]


def test_bottom_icon_threshold_logic():
    """判定阈值常量化：BOTTOM_ICON_THRESHOLD 可被测试引用（>=20 像素）"""
    assert bv.BOTTOM_ICON_THRESHOLD == 20
