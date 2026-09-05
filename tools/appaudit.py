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
  （appaudit.rs::strictly_under，本文件保留 Python 版供 oracle 对照）。
  asar 提取（SHA256 自标定）随实现整体入 Rust，sha256.rs 为手写零依赖实现。

S86（2026-09-05）：app_clone / app_clean Rust 原生化（rx-appops.exe，见
  rust/src/appclone.rs）——appaudit 域 3/3 全薄壳收官。授权门仍留 Python registry
  （requires_auth + __authorized，exe 永不自行放权）；沙盒门两语言各一版（oracle
  对照钉死）。Rust walk 按 Python 3.14 实测真值（oracle 探针钉死）：junction 不再
  是 symlink（islink=False、悬空也算目录）——有效 junction 照进并克隆目标内容、
  悬空 read_dir 失败静默剪枝、均不进 skipped_links（那只数真 symlink）；junction
  判别走 reparse tag 手写 FFI（Rust read_link 已剥设备前缀，文本不可判别）；
  os.path.relpath 的 Win32 GetFullPathName 归一（成分尾部空格/点剥除）在 errors
  显示侧复刻；时间戳走 GetLocalTime FFI（零 crate 红线）。本文件保留
  _sandbox_root/_strictly_under 作 oracle 锚与沙盒纪律文档。
"""
import json
import os
import subprocess
from pathlib import Path

from registry import tool

_SANDBOX_ENV = "UNIFIED_RX_AUDIT_SANDBOX"

_RX_AUDIT_EXE_NAME = "rx-audit.exe"
_RX_APPOPS_EXE_NAME = "rx-appops.exe"


def _rs_exe(exe_name):
    """定位域 exe（rx-audit / rx-appops 共用）：UNIFIED_RX_RS_EXE 覆盖 → cargo
    目标目录惯例路径。

    与 tools/astscan.py::_rx_scan_exe 同纪律：候选必须是已存在且文件名恰为
    该 exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, exe_name)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == exe_name:
            return c
    return None


def _rx_audit_exe():
    return _rs_exe(_RX_AUDIT_EXE_NAME)


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


def _rx_appops_call(op, *args):
    """薄壳转调 rx-appops.exe（S86，tools/fs.py::_rx_fs_call 同款子命令形态）。

    退出码契约同 rx-audit：0 = 工具级结果（含 {"error": ...} 包络，原样透传）；
    2 = 用法级拒绝 → ValueError；其他非零 = 执行失败 → ValueError。
    max_files/max_bytes 经 registry schema 门已是 int，此处 str() 进 argv。
    """
    exe = _rs_exe(_RX_APPOPS_EXE_NAME)
    if not exe:
        raise ValueError("rx-appops.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, op, *[str(a) for a in args]]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-appops 超时（600s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-appops 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-appops 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-appops 执行失败（exit={cp.returncode}）: {tail}")
    return out


def _rx_appops_exe():
    return _rs_exe(_RX_APPOPS_EXE_NAME)


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
    """审计沙箱根（S86 起与 rust/src/appclone.rs::sandbox_root 双语言各一版，
    生产路径走 exe；Python 版保留作 oracle 对照锚与沙盒纪律文档）。"""
    base = os.environ.get(_SANDBOX_ENV) or os.path.join(
        os.environ.get("TEMP", r"C:\Temp"), "unified-rx-appaudit")
    root = Path(os.path.abspath(base))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _strictly_under(p, root=None):
    """normcase 严格子判定：拒绝空白/等于根本身/大小写欺骗。

    S86 起生产路径（app_clean）走 rx-appops.exe 内同语义 Rust 版
    （appaudit.rs::strictly_under）；Python 版保留作 oracle 对照锚。
    """
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
    # S86：克隆引擎整体在 rx-appops.exe（rust/src/appclone.rs）——绝对路径/存在性/
    # 目录判定/预算 int() 语义/junction walk/清单指纹/验证全在 Rust 侧
    return _rx_appops_call("clone", source_dir.strip(), max_files, max_bytes)


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
    # S86：沙盒门（strictly_under 等价复刻）与移除都在 rx-appops.exe 侧
    return _rx_appops_call("clean", target.strip())
