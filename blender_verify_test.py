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


def test_ocr_file_tolerates_down_server():
    """Umi-OCR 未启动时容错（返回错误行而非崩溃）"""
    lines = bv.ocr_file(bv.OUT_DEFAULT)
    assert isinstance(lines, list)
