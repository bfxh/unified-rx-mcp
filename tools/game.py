# -*- coding: utf-8 -*-
"""tools/game.py —— 游戏域（2 工具）：game_check / blender_verify

P3 增强（2026-08-24）：blender_verify 补 Umi-OCR 读界面文字（HTTP API 或 CLI）。
"""
import os
import re
import json
import subprocess
import urllib.request
import urllib.error

from registry import tool
from tools.fs import _resolve as _fs_resolve

UMI_EXE = r"D:\rj\GJ\Umi-OCR_Paddle_v2.1.5\Umi-OCR.exe"
UMI_HTTP_PORTS = (1224, 1225)


def _ps_quote(path):
    """S75：PowerShell 单引号字面量转义——'' 是唯一转义序列。

    $bmp.Save('{shot}') 把路径拼进单引号字符串，路径含 ' 时原样拼接即可
    逃逸字符串注入任意 PS 命令；转义后注入串变纯文件名字面量。
    """
    return path.replace("'", "''")


def _umi_ocr_image(img_path):
    """Umi-OCR 读图：先试 HTTP API（已运行），再试命令行模式。返回 (ok, text)。"""
    # 1. HTTP API（Umi-OCR 运行时默认端口）
    for port in UMI_HTTP_PORTS:
        try:
            # Umi-OCR HTTP API: POST /api/ocr 带图片路径或 base64
            payload = json.dumps({"image_path": img_path}).encode("utf-8") if False else None
            # 常见端点探测
            for endpoint in ("/api/ocr", "/ocr", "/api/v1/ocr"):
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{endpoint}",
                        data=b'{"image_path": "' + img_path.replace("\\", "/").encode() + b'"}',
                        headers={"Content-Type": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    data = resp.read().decode("utf-8", errors="replace")
                    import json as _j
                    try:
                        d = _j.loads(data)
                        texts = []
                        for r in d.get("data", []) if isinstance(d.get("data"), list) else []:
                            texts.append(r.get("text", ""))
                        if texts:
                            return True, "\n".join(texts)
                    except Exception:
                        if data.strip():
                            return True, data[:1000]
                except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                    continue
        except Exception:
            continue
    # 2. 命令行模式（Umi-OCR 支持 --ocr 参数，未运行时启动较慢）
    if os.path.exists(UMI_EXE):
        try:
            r = subprocess.run([UMI_EXE, "--ocr", img_path], capture_output=True,
                               text=True, timeout=60,
                               env={**os.environ, "PYTHONUTF8": "1"})
            if r.returncode == 0 and r.stdout.strip():
                return True, r.stdout.strip()[:2000]
        except Exception:
            pass
    return False, "Umi-OCR 不可用（未运行或未配置）"


@tool("game_check", "游戏规则检查（模块化/连接点/按键覆盖）", "game",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "游戏文件或目录"},
           "action": {"type": "string", "description": "check/rules/verify（默认 check）"},
       },
       "required": ["path"]})
def game_check(path, action="check"):
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    findings = []
    if os.path.isdir(path):
        files = []
        for r, dirs, fs in os.walk(path):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                    "__pycache__", "dist", "build",
                                                    ".codegraph", "backups")]
            for fn in fs:
                if fn.endswith((".rs", ".gd", ".py")):
                    files.append(os.path.join(r, fn))
    else:
        files = [path]
    key_bindings = []
    for fp in files[:60]:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for m in re.finditer(r"(KeyCode|MouseButton|key)::([A-Za-z0-9]+)", src):
            key_bindings.append({"file": fp, "key": m.group(2)})
        for m in re.finditer(r"=\s*(-?\d{3,})\b", src):
            findings.append({"file": fp, "rule": "magic_number", "value": m.group(1)})
    seen = set()
    unique_keys = []
    for kb in key_bindings:
        if kb["key"] not in seen:
            seen.add(kb["key"])
            unique_keys.append(kb)
    return {
        "files": len(files),
        "key_bindings": unique_keys[:30],
        "key_count": len(unique_keys),
        "findings": findings[:50],
        "summary": f"{len(unique_keys)} 键位绑定 / {len(findings)} 魔法数字",
    }


@tool("blender_verify", "Blender 窗口实地验证（截图+工具栏检查+Umi-OCR；需 Blender 运行中）", "game",
      {"type": "object",
       "properties": {
           "ocr": {"type": "boolean", "description": "是否调 Umi-OCR 读界面文字（需 Umi-OCR 运行中）"},
           "screenshot_path": {"type": "string", "description": "截图保存路径（默认 D:\\开发\\blender_verify.png）"},
       },
       "required": []},
      requires_auth=True)  # S75：全屏截屏 + powershell 注入面，高危须显式授权
def blender_verify(ocr=False, screenshot_path=None, __authorized=False):
    del __authorized  # S75：全屏截屏=隐私面 + spawn powershell，执行授权由 registry.call 统一强制
    # S75：显式路径过沙盒；默认路径是固定可信常量免检（与 S73 lesson 同纪律）
    if screenshot_path:
        try:
            shot = _fs_resolve(screenshot_path)
        except ValueError as e:
            return {"error": str(e)}
    else:
        shot = r"D:\开发\blender_verify.png"
    blenders = []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq blender.exe"],
                             capture_output=True, text=True, timeout=15,
                             encoding="gbk", errors="replace")
        for line in out.stdout.split("\n"):
            if "blender.exe" in line.lower():
                blenders.append(line.strip())
    except Exception as e:
        return {"ok": False, "error": f"tasklist 失败: {e}"}
    if not blenders:
        return {"ok": False, "note": "Blender 未运行（无法实地验证）", "blender_processes": []}
    shot_ok = False
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
            "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
            f"$bmp.Save('{_ps_quote(shot)}');"
        )
        r = subprocess.run(["powershell", "-Command", ps], capture_output=True,
                           timeout=20, encoding="gbk", errors="replace")
        shot_ok = r.returncode == 0
    except Exception:
        shot_ok = False
    # Umi-OCR（P3 增强）
    ocr_text = None
    if ocr and shot_ok:
        ok, text = _umi_ocr_image(shot)
        ocr_text = text if ok else None
    return {
        "ok": True,
        "blender_processes": blenders,
        "screenshot": shot if shot_ok else None,
        "ocr_enabled": ocr,
        "ocr_text": ocr_text,
        "note": "截图已保存；OCR 需 Umi-OCR 运行中（HTTP 或 CLI）",
    }
