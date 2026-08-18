# -*- coding: utf-8 -*-
"""裁剪截图底部工具栏区域并放大——验证'游戏'工具图标文字。"""
import sys
import os

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_verify as bv


def main():
    src = bv.OUT_DEFAULT
    img = Image.open(src).convert("RGB")
    w, h = img.size
    # 左侧工具栏底部 1/8 区域 → 放大 4 倍
    tb_w = min(64, w)
    bottom = img.crop((0, int(h * 0.875), tb_w, h))
    big = bottom.resize((tb_w * 4, int(h * 0.125 * 4)), Image.LANCZOS)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "toolbar_bottom_zoom.png")
    big.save(out)
    print(f"ZOOMED: {out} ({big.size[0]}x{big.size[1]})")
    lines = bv.ocr_file(out)
    print(f"OCR({len(lines)}行): {lines[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
