# -*- coding: utf-8 -*-
"""Blender 窗口截图 + 工具栏验证（2026-08-19 实地调查工具）。

用途：AI 无法直接看屏幕——此脚本截取 Blender 窗口 → 检查左侧工具栏
底部是否有"游戏"工具（白字图标 → 白色像素簇）+ OCR 文本输出。

用法：
    python blender_verify.py [--ocr] [--out <png路径>]
"""
import os
import sys

# M1 修复（mcp-developer 审查 2026-08-19）：Windows 管道下默认 GBK 输出
# → 父进程 utf-8 解码中文全乱码。强制 UTF-8 输出（双保险：env 见 server.py）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "blender_verify.png")

# 底部图标判定阈值（底部两段白色像素和 > 阈值 = 有图标）——常量供测试引用
BOTTOM_ICON_THRESHOLD = 20


def bottom_icon_yes(bottom_white):
    """底部图标判定（可测纯函数——main 与测试共用，防判定方向写反）"""
    return bottom_white > BOTTOM_ICON_THRESHOLD


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
    px = list(tb.get_flattened_data())
    white = sum(1 for p in px if p > 150)
    total = len(px)
    # 分 8 段看分布
    segs = []
    for i in range(8):
        y0 = i * h // 8
        y1 = (i + 1) * h // 8
        seg = tb.crop((0, y0, tb_w, y1))
        sp = list(seg.get_flattened_data())
        segs.append(sum(1 for p in sp if p > 150))
    return {"white_ratio": white / total, "segments": segs, "tb_width": tb_w}


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
    # build_opener 也放入 try（opener 构造失败同样容错——测试暴露）
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
        if args.index("--out") + 1 >= len(args):
            print("用法: --out <png路径>")
            return 2
        out = args[args.index("--out") + 1]
    # 纯 OCR 模式：只读已有截图
    if "--ocr-file" in args:
        if args.index("--ocr-file") + 1 >= len(args):
            print("用法: --ocr-file <png路径>")
            return 2
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
    print(f"BOTTOM_ICON: {'YES' if bottom_icon_yes(bottom_white) else 'NO'} "
          f"(底部白色像素={bottom_white})")
    if do_ocr:
        lines = ocr_file(out)
        print(f"OCR({len(lines)}行): {lines[:20]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
