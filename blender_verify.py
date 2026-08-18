# -*- coding: utf-8 -*-
"""Blender 窗口截图 + 工具栏验证（2026-08-19 实地调查工具）。

用途：AI 无法直接看屏幕——此脚本截取 Blender 窗口 → 检查左侧工具栏
底部是否有"游戏"工具（白字图标 → 白色像素簇）+ OCR 文本输出。

用法：
    python blender_verify.py [--ocr] [--out <png路径>]
"""
import os
import sys

OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "blender_verify.png")


def find_blender_window():
    """win32gui 找 Blender 主窗口 → (hWnd, rect) 或 None"""
    import win32gui
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "Blender" in title:
                found.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    if not found:
        return None
    # 取最大的（主窗口）
    best = None
    best_area = 0
    for hwnd, title in found:
        rect = win32gui.GetWindowRect(hwnd)
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area > best_area:
            best_area = area
            best = (hwnd, title, rect)
    return best


def capture(rect, out_path):
    """截取窗口区域 → 保存 PNG → 返回 PIL Image"""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=rect)
    img.save(out_path)
    return img


def analyze_toolbar(img):
    """分析左侧工具栏（宽 ~60px 竖条）：白色像素分布 + 底部是否有图标"""
    from PIL import Image
    gray = img.convert("L")
    w, h = gray.size
    tb_w = 64  # 工具栏宽度（Blender 默认 ~50-64px）
    tb = gray.crop((0, 0, min(tb_w, w), h))
    px = list(tb.getdata())
    white = sum(1 for p in px if p > 150)
    total = len(px)
    # 分 8 段看分布
    segs = []
    for i in range(8):
        y0 = i * h // 8
        y1 = (i + 1) * h // 8
        seg = tb.crop((0, y0, tb_w, y1))
        sp = list(seg.getdata())
        segs.append(sum(1 for p in sp if p > 150))
    return {"white_ratio": white / total, "segments": segs, "tb_width": tb_w}


def ocr(img):
    """调本地 Umi-OCR HTTP API（127.0.0.1:1224）→ 文本行"""
    import base64
    import io
    import json
    import urllib.request
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    payload = json.dumps({"base64": base64.b64encode(buf.getvalue()).decode()}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:1224/api/ocr", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    lines = []
    for item in data.get("data", []):
        t = item.get("text", "").strip()
        if t:
            lines.append(t)
    return lines


def ocr_file(path):
    """对已保存的 PNG 调 Umi-OCR HTTP API → 文本行"""
    import base64
    import json
    import urllib.request
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({"base64": b64}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:1224/api/ocr", data=payload,
        headers={"Content-Type": "application/json"})
    # 禁用系统代理（curl 直连正常但 urllib 走代理被拒——2026-08-19 实地发现）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return [f"<OCR 失败: {e}>"]
    lines = []
    for item in data.get("data", []):
        t = item.get("text", "").strip()
        if t:
            lines.append(t)
    return lines


def main():
    args = sys.argv[1:]
    do_ocr = "--ocr" in args
    out = OUT_DEFAULT
    if "--out" in args:
        out = args[args.index("--out") + 1]
    # 纯 OCR 模式：只读已有截图
    if "--ocr-file" in args:
        path = args[args.index("--ocr-file") + 1]
        lines = ocr_file(path)
        print(f"OCR({len(lines)}行): {lines[:25]}")
        return 0

    win = find_blender_window()
    if win is None:
        print("RESULT: NO_BLENDER_WINDOW")
        return 2
    hwnd, title, rect = win
    print(f"WINDOW: {title} rect={rect}")
    img = capture(rect, out)
    print(f"CAPTURED: {out} ({img.size[0]}x{img.size[1]})")
    info = analyze_toolbar(img)
    print(f"TOOLBAR: 白字占比={info['white_ratio']:.1%} "
          f"段分布(上→下)={info['segments']}")
    # 底部两段（6/7 段）有白色 → 底部有图标
    bottom_white = info["segments"][6] + info["segments"][7]
    print(f"BOTTOM_ICON: {'YES' if bottom_white > 20 else 'NO'} "
          f"(底部白色像素={bottom_white})")
    if do_ocr:
        lines = ocr_file(out)
        print(f"OCR({len(lines)}行): {lines[:20]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
