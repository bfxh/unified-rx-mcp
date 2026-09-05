# -*- coding: utf-8 -*-
"""tools/appaudit.py —— S8 智能体/桌面应用自查域（3 工具）：app_clone / app_audit / app_clean

用户规则固化（2026-08-27）：遇到任何已安装智能体或桌面应用 → 先克隆到隔离沙箱，
审计只碰副本。原件零接触。

隔离三保证：
1. 克隆唯一落点 %TEMP%\\unified-rx-appaudit\\<时间戳>-<sha256前12位>-<净化名>\\，
   目录名由哈希派生，不吃原始路径的注入（对比 learn 过的 normpath 空白放大漏洞）
2. app_audit 强制只接受沙箱内路径（normcase + 严格子判定）——想直接审原件 = 结构性拒绝
3. app_clean 同样强制在沙箱内 + requires_auth（声明式授权，registry 层统一把关）

S85（2026-09-05）：app_audit Rust 原生化（rx-audit.exe，见 rust/src/appaudit.rs）——
  Python 侧只留薄壳转调，exe 缺失报清晰错误不静默降级；沙盒门在 Rust 侧等价复刻
  （appaudit.rs::strictly_under，本文件保留 Python 版供 app_clean 与 oracle 对照）。
  asar 提取（SHA256 自标定）随实现整体入 Rust，sha256.rs 为手写零依赖实现。
  app_clone / app_clean 是写面+授权门，按"纯读先迁"纪律后置。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from registry import tool

_SANDBOX_ENV = "UNIFIED_RX_AUDIT_SANDBOX"

_RX_EXE_NAME = "rx-audit.exe"


def _rx_audit_exe():
    """定位 rx-audit.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/astscan.py::_rx_scan_exe 同纪律：候选必须是已存在且文件名恰为
    rx-audit.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_EXE_NAME:
            return c
    return None


def _rx_audit_call(snapshot_dir, with_asar):
    """薄壳转调 rx-audit.exe，返回结果 dict；用法级拒绝 raise ValueError。

    退出码契约：0 = 工具级结果（含 {"error": ...} 包络，原样透传）；
    2 = 用法级拒绝 → ValueError；其他非零 = 执行失败 → ValueError。
    """
    exe = _rx_audit_exe()
    if not exe:
        raise ValueError("rx-audit.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, snapshot_dir, "1" if with_asar else "0"]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-audit 超时（600s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-audit 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-audit 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-audit 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("app_audit", "审计隔离沙箱内的克隆（拒绝原件路径）：JS危险面/秘密掩码采集/URL清单/二进制盘点/asar提取后复扫", "appaudit",
      {"type": "object",
       "properties": {
           "snapshot_dir": {"type": "string", "description": "app_clone 返回的 snapshot 路径"},
           "with_asar": {"type": "boolean", "description": "是否提取 .asar 文本条目再扫（默认 true）"},
       },
       "required": ["snapshot_dir"]})
def app_audit(snapshot_dir, with_asar=True):
    if not isinstance(snapshot_dir, str) or not snapshot_dir.strip():
        return {"error": "snapshot_dir 必须是非空字符串"}
    return _rx_audit_call(snapshot_dir, bool(with_asar))


def _sandbox_root():
    base = os.environ.get(_SANDBOX_ENV) or os.path.join(
        os.environ.get("TEMP", r"C:\Temp"), "unified-rx-appaudit")
    root = Path(os.path.abspath(base))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _strictly_under(p, root=None):
    """normcase 严格子判定：拒绝空白/等于根本身/大小写欺骗。"""
    root = root or _sandbox_root()
    if not isinstance(p, str) or not p.strip():
        return False
    try:
        rp = Path(os.path.abspath(p)).resolve(strict=False)
    except OSError:
        return False
    pc = os.path.normcase(str(rp))
    rc = os.path.normcase(str(Path(root).resolve(strict=False)))
    return pc != rc and pc.startswith(rc.rstrip("\\") + "\\")


@tool("app_clone", "克隆已安装应用到隔离审计沙箱（唯一落点，防名注入；返回清单指纹）。审原件请先克隆。", "appaudit",
      {"type": "object",
       "properties": {
           "source_dir": {"type": "string", "description": "待审计应用的安装目录（绝对路径）"},
           "max_files": {"type": "integer", "description": "文件数上限（默认 20000）"},
           "max_bytes": {"type": "integer", "description": "总字节数上限（默认 3GB）"},
       },
       "required": ["source_dir"]},
      requires_auth=True)
def app_clone(source_dir, max_files=20000, max_bytes=3 * 1024 * 1024 * 1024,
              __authorized=False):
    del __authorized  # S73：整目录读取=隐私面（fs_read 够不着的沙盒外目录），授权由 registry 统一强制
    if not isinstance(source_dir, str) or not source_dir.strip():
        return {"error": "source_dir 必须是非空字符串"}
    src = Path(source_dir.strip())
    if not src.is_absolute():
        return {"error": "必须绝对路径（防相对路径歧义）；例：D:\\rj\\AI\\Yan Agent"}
    try:
        src_real = src.resolve(strict=True)
    except OSError:
        return {"error": f"源不存在或不可达: {src}"}
    if not src_real.is_dir():
        return {"error": f"不是目录: {src_real}"}
    max_files = int(max_files)
    max_bytes = int(max_bytes)

    tag = hashlib.sha256(os.path.normcase(str(src_real)).encode()).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", src_real.name)[:40] or "app"
    root = _sandbox_root()
    stem = time.strftime("%Y%m%d-%H%M%S") + "-" + tag
    dest = root / (stem + "-" + safe_name)
    k = 1
    while dest.exists():
        k += 1
        dest = root / (stem + f"-{k}-{safe_name}")
    dest.mkdir()

    manifest = hashlib.sha256()
    copied = copied_bytes = skipped_links = meta_warns = read_fails = 0
    truncation = None
    errors = []
    plan = []            # (rel, size) 内容写入成功项，验证阶段对照
    stopped = False
    for dirpath, dirnames, filenames in os.walk(src_real):
        if stopped:
            break
        rel_dir = os.path.relpath(dirpath, src_real)
        out_dir = dest if rel_dir == "." else dest / rel_dir
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            errors.append(f"{rel_dir}: mkdir {e.__class__.__name__}")
            dirnames[:] = []
            continue
        for fn in filenames:
            fpath = os.path.join(dirpath, fn)
            if os.path.islink(fpath):
                skipped_links += 1
                continue
            try:
                st = os.stat(fpath)
                if copied >= max_files:
                    truncation = "max_files"
                    stopped = True
                    break
                if copied_bytes + st.st_size > max_bytes:
                    truncation = "max_bytes"
                    stopped = True
                    break
                rel = "" if rel_dir == "." else os.path.join(rel_dir, fn)
                dst_path = os.path.join(out_dir, fn)
                # 分级：内容复制失败才算丢文件；元数据（mtime 等）失败只计警告。
                # 实测教训：锁定中的 cache 文件必炸；且必须【先开源句柄】——
                # shutil.copyfile 先建目标，源打不开会留下 0 字节残桩污染克隆。
                try:
                    with open(fpath, "rb") as fin:
                        with open(dst_path, "wb") as fout:
                            shutil.copyfileobj(fin, fout, 8 * 1024 * 1024)
                except OSError:
                    try:
                        if os.path.exists(dst_path):
                            os.remove(dst_path)
                    except OSError:
                        pass
                    raise
                try:
                    shutil.copystat(fpath, dst_path, follow_symlinks=False)
                except OSError:
                    meta_warns += 1
                copied += 1
                copied_bytes += st.st_size
                plan.append((rel.replace("\\", "/"), st.st_size))
                manifest.update(f"{rel.replace(chr(92), '/')}\t{st.st_size}\n".encode())
            except OSError as e:
                read_fails += 1
                if len(errors) < 30:
                    errors.append(f"{os.path.relpath(fpath, src_real)}: {e.__class__.__name__}")

    # 验证阶段：副本实盘复核（计数不一致必须显式暴露，不静默）
    v_files = v_bytes = 0
    for dp, dns, fns in os.walk(dest):
        for fn in fns:
            try:
                v_files += 1
                v_bytes += os.stat(os.path.join(dp, fn)).st_size
            except OSError:
                pass

    return {
        "snapshot": str(dest),
        "source": str(src_real),
        "files": copied,
        "bytes": copied_bytes,
        "verify_files": v_files,
        "verify_bytes": v_bytes,
        "verified": bool(v_files == len(plan) and v_bytes == copied_bytes),
        "inventory_digest": manifest.hexdigest(),
        "skipped_links": skipped_links,
        "meta_warns": meta_warns,
        "read_fails": read_fails,     # 内容级失败总数（errors 仅前 30 条样本）
        "errors": errors,
        "truncated_by": truncation,
    }


@tool("app_clean", "清理隔离沙箱内的克隆（requires_auth；严格限沙箱内，越界一律拒绝）", "appaudit",
      {"type": "object",
       "properties": {
           "target": {"type": "string", "description": "app_clone 返回的 snapshot 路径"},
       },
       "required": ["target"]},
      requires_auth=True)
def app_clean(target, __authorized=False):
    del __authorized
    if not isinstance(target, str) or not target.strip():
        return {"error": "target 必须是非空字符串"}
    if not _strictly_under(target):
        return {"error": "拒绝：清理目标必须在隔离沙箱内（app_clone 的 snapshot 路径）"}
    p = Path(os.path.abspath(target)).resolve(strict=False)
    try:
        shutil.rmtree(p)
        return {"removed": True, "path": str(p)}
    except OSError as e:
        return {"error": f"清理失败: {e.__class__.__name__}: {e}（沙箱内可手动重试；多半是文件被占用）"}
