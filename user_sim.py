#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""user_sim —— 用户操作模拟（2026-08-23，用户：模拟用户怎么操作的，高压常态）。

零依赖（ctypes user32）Windows 操作模拟：窗口激活/移动/点击/输入/组合键/等待/
截图。用于 UI 冒烟与回归：按用户操作序列真实驱动桌面应用 → 截图留证 →
配合 OCR（blender_verify 链路）断言界面状态。

操作序列（JSON，最多 100 步）：
    {"action": "window", "title": "Blender", "class": "可选"}   # 激活窗口
    {"action": "move", "x": 100, "y": 200}                       # 移动鼠标
    {"action": "click", "x": 100, "y": 200, "button": "left|right|middle",
     "double": false}                                            # 点击
    {"action": "type", "text": "hello"}                          # 键盘输入
    {"action": "key", "keys": "ctrl+s"}                          # 组合键
    {"action": "wait", "ms": 500}                                # 等待
    {"action": "screenshot", "path": "shot.png"}                 # 全屏截图

用法：
    python user_sim.py run --script actions.json [--shot out.png]
    python user_sim.py window "Blender"                          # 列出/激活窗口
CLI:  python cli.py user-sim --script actions.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

# ── Windows API 常量 ──
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_KEYEVENTF_KEYUP = 0x0002
_VK_MODS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
            "win": 0x5B, "meta": 0x5B, "esc": 0x1B, "escape": 0x1B,
            "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
            "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "up": 0x26,
            "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24,
            "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
            "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
            "f11": 0x7A, "f12": 0x7B}
_MAX_STEPS = 100
_MAX_TEXT = 500

# 控制台编码兜底（Windows GBK 控制台打印中文/特殊字符不崩）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class UserSimError(Exception):
    pass


def _key_code(ch: str) -> int:
    """字符 → 虚拟键码（VkKeyScanW 高字节=shift 状态）。"""
    vk = user32.VkKeyScanW(ord(ch)) & 0xFF
    return vk or 0


def activate_window(title: str, class_name: str = "") -> bool:
    """按标题/类名激活窗口。返回是否找到并激活。"""
    hwnd = user32.FindWindowW(class_name or None, title)
    if not hwnd:
        # 模糊匹配：遍历顶层窗口找标题包含目标
        hwnd = _find_window_contains(title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    return True


def _find_window_contains(substr: str):
    found = []

    def _cb(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if substr.lower() in buf.value.lower():
            found.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return next(iter(found), 0)  # 首个匹配窗口（无匹配返回 0）


def list_windows() -> list[dict]:
    """列出顶层窗口（标题非空）。"""
    out = []

    def _cb(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": hwnd, "title": buf.value,
                    "x": rect.left, "y": rect.top,
                    "w": rect.right - rect.left, "h": rect.bottom - rect.top})
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out[:50]


def move_mouse(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    move_mouse(x, y)
    down, up = {
        "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
        "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
        "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
    }[button]
    if double:
        for _ in range(2):
            user32.mouse_event(down, 0, 0, 0, 0)
            user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)
    else:
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(up, 0, 0, 0, 0)
    time.sleep(0.1)


def press_key(vk: int, shift: bool = False) -> None:
    if shift:
        user32.keybd_event(_VK_MODS["shift"], 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    if shift:
        user32.keybd_event(_VK_MODS["shift"], 0, _KEYEVENTF_KEYUP, 0)


def type_text(text: str) -> None:
    if len(text) > _MAX_TEXT:
        raise UserSimError(f"text 超长（>{_MAX_TEXT}）")
    for ch in text:
        if ch == "\n":
            press_key(_VK_MODS["enter"])
            continue
        scan = user32.VkKeyScanW(ord(ch))
        vk = scan & 0xFF
        needs_shift = (scan >> 8) & 1
        if vk:
            press_key(vk, shift=bool(needs_shift))
        time.sleep(0.02)


def combo(keys: str) -> None:
    """组合键：'ctrl+s' / 'alt+f4' / 'shift+enter'。"""
    parts = [p.strip().lower() for p in keys.split("+")]
    if not parts:
        raise UserSimError("keys 为空")
    main = parts[-1]
    mods = [_VK_MODS[p] for p in parts[:-1] if p in _VK_MODS]
    if main in _VK_MODS:
        vk = _VK_MODS[main]
        mods = mods[:-1] if mods else []
    elif len(main) == 1:
        vk = _key_code(main)
    else:
        raise UserSimError(f"未知按键: {main}")
    for m in mods:
        user32.keybd_event(m, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        user32.keybd_event(m, 0, _KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def screenshot(path: str) -> None:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(path)
    except Exception as exc:  # noqa: BLE001
        raise UserSimError(f"截图失败（需 PIL）: {exc}") from exc


def run_actions(actions: list[dict], shot_path: str = "") -> dict:
    """执行操作序列。返回 {ok, steps, results, errors, elapsed_ms}"""
    if not isinstance(actions, list) or not actions:
        raise UserSimError("actions 必须是非空数组")
    if len(actions) > _MAX_STEPS:
        raise UserSimError(f"操作步数超限（>{_MAX_STEPS}）")
    results: list[dict] = []
    errors: list[dict] = []
    t0 = time.perf_counter()
    for idx, step in enumerate(actions):
        action = str(step.get("action", "")).strip()
        try:
            if action == "window":
                ok = activate_window(str(step.get("title", "")),
                                     str(step.get("class", "")))
                results.append({"step": idx, "action": action,
                                "ok": ok, "detail": "窗口已激活" if ok else "窗口未找到"})
            elif action == "move":
                move_mouse(int(step["x"]), int(step["y"]))
                results.append({"step": idx, "action": action, "ok": True})
            elif action == "click":
                click(int(step["x"]), int(step["y"]),
                      str(step.get("button", "left")),
                      bool(step.get("double", False)))
                results.append({"step": idx, "action": action, "ok": True})
            elif action == "type":
                type_text(str(step.get("text", "")))
                results.append({"step": idx, "action": action, "ok": True})
            elif action == "key":
                combo(str(step.get("keys", "")))
                results.append({"step": idx, "action": action, "ok": True})
            elif action == "wait":
                time.sleep(max(0, min(int(step.get("ms", 100)), 30000)) / 1000.0)
                results.append({"step": idx, "action": action, "ok": True})
            elif action == "screenshot":
                p = str(step.get("path", shot_path))
                if not p:
                    raise UserSimError("screenshot 需要 path")
                screenshot(p)
                results.append({"step": idx, "action": action, "ok": True,
                                "detail": p})
            else:
                raise UserSimError(f"未知操作: {action}")
        except Exception as exc:  # noqa: BLE001
            errors.append({"step": idx, "action": action,
                           "error": str(exc)[:200]})
    # 收尾截图（shot_path 且未在序列里截过）
    if shot_path and not any(r.get("action") == "screenshot" for r in results):
        try:
            screenshot(shot_path)
            results.append({"step": "final", "action": "screenshot",
                            "ok": True, "detail": shot_path})
        except Exception as exc:  # noqa: BLE001
            errors.append({"step": "final", "action": "screenshot",
                           "error": str(exc)[:200]})
    return {
        "ok": not errors,
        "steps": len(actions),
        "results": results,
        "errors": errors[:10],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "hint": "操作模拟完成——配合截图+OCR 断言界面状态（高压/冒烟常态化）",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="用户操作模拟（零依赖 Windows）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="执行操作序列")
    p.add_argument("--script", required=True, help="操作序列 JSON 文件")
    p.add_argument("--shot", default="", help="收尾截图路径")
    p.set_defaults(fn=lambda a: print(json.dumps(
        run_actions(json.load(open(a.script, encoding="utf-8")), a.shot),
        ensure_ascii=False, indent=1)) or 0)
    p = sub.add_parser("window", help="列出/激活窗口")
    p.add_argument("title", nargs="?", default="", help="激活的窗口标题（可选）")
    p.set_defaults(fn=lambda a: (
        print(json.dumps(list_windows(), ensure_ascii=False, indent=1))
        if not a.title else
        print(f"激活: {activate_window(a.title)}")) or 0)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
