# -*- coding: utf-8 -*-
"""tools/fs.py —— 文件层（4 工具）：fs_read / fs_write / fs_stat / fs_list

S79 起读面三工具（fs_read/fs_stat/fs_list）由 Rust 原生实现承担；S90 fs_write 收官——
fs 域 4/4 全部薄壳转调 rx-fs.exe（spec/VULN-HUNTING.md 五·Rust 迁移路线图）。包络契约与旧实现对齐：
- 沙盒拒绝（resolve 层）→ exe 退出码 2 → 壳 raise ValueError → registry 包成
  ok:false（旧实现抛 ValueError 同包络）；
- 工具级结果（不是文件/过大/不是目录）→ exe 退出码 0 + result.error 字段
  （旧实现返回 dict 同包络）；
- exe 缺失/超时/非 JSON 输出 → 清晰报错，不静默降级回 Python 实现。

安全设计（吸取旧版教训）：
- 沙盒：UNIFIED_RX_SANDBOX 环境变量（分号分隔多个根），Python/Rust 两侧同语义
- fail-closed：未设置 = 一律拒绝；"*" = 显式放开（自检/可信宿主用）
- 写保护：fs_write 需 __authorized=True（显式授权，防 AI 幻觉乱写）
- 大小上限：读 ≤1MB，写 ≤1MB
- 路径校验：解析绝对路径后校验沙盒前缀，realpath 防 symlink 出逃
"""
import json
import os
import subprocess

from registry import tool

SANDBOX_ENV = "UNIFIED_RX_SANDBOX"
MAX_BYTES = 1_000_000


def _sandbox_roots():
    env = os.environ.get(SANDBOX_ENV, "")
    if not env:
        return []  # fail-closed：未配置 = 一律拒绝，杜绝忘配 env 导致裸奔
    if env.strip() == "*":
        return None  # 显式放开（自检/明确可信的宿主）
    roots = []
    for p in env.split(";"):
        p = p.strip()
        if p:
            # 仅收非空条目：防空白/垃圾值经 normpath 落到 cwd 意外放大沙盒
            roots.append(os.path.abspath(p))
    return roots  # 全垃圾 = 空列表 = 一律拒绝


def _in_sandbox(path):
    roots = _sandbox_roots()
    if roots is None:
        return True
    if not roots:
        return False
    # P2 修复：realpath 解析符号链接/junction（防沙盒内 symlink 指向沙盒外）
    rp = os.path.realpath(path)
    for r in roots:
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _resolve(path):
    """校验沙盒 + 返回绝对路径。越界抛 ValueError。"""
    if not isinstance(path, str) or not path:
        raise ValueError("path 必填")
    ap = os.path.abspath(path)
    if not _in_sandbox(ap):
        raise ValueError(f"路径越界（沙盒外）: {path}")
    # 返回 realpath（防后续 open() 走符号链接）
    return os.path.realpath(ap)


_RX_EXE_NAME = "rx-fs.exe"


def _rx_fs_exe():
    """定位 rx-fs.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    候选必须是已存在且文件名恰为 rx-fs.exe 的常规文件（与 _rx_taint_exe 同纪律：
    argv 固定前缀、list 形式、无 shell，env 覆盖不构成任意命令执行面）。
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


def _rx_fs_call(op, path, depth=None, stdin_bytes=None):
    """薄壳转调 rx-fs.exe，返回结果 dict；resolve 层拒绝 raise ValueError。

    S90：stdin_bytes 非 None 时走二进制字节通道（input=bytes、不设 text=True）——
    text 模式 stdin 会做 \\n→os.linesep 换行翻译（S90 探针实锤），写内容必须字节
    保真；stdout/stderr 统一按 utf-8/replace 手工解码，解析逻辑两路共用。
    """
    exe = _rx_fs_exe()
    if not exe:
        raise ValueError("rx-fs.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, op, path]
    if depth is not None:
        argv.append(str(depth))
    try:
        if stdin_bytes is not None:
            cp = subprocess.run(argv, capture_output=True, timeout=120,
                                input=stdin_bytes)
            stdout = (cp.stdout or b"").decode("utf-8", errors="replace")
            stderr = (cp.stderr or b"").decode("utf-8", errors="replace")
        else:
            cp = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
            stdout = cp.stdout or ""
            stderr = cp.stderr or ""
    except subprocess.TimeoutExpired:
        raise ValueError("rx-fs 超时（120s）")
    tail = stderr.strip()[-300:]
    lines = stdout.strip().splitlines()
    if not lines:
        raise ValueError(f"rx-fs 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-fs 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # resolve 层拒绝（沙盒越界/未配置/path 必填）→ 与旧实现同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-fs 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("fs_read", "安全读取文件（≤1MB，沙盒校验）", "fs",
      {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}},
       "required": ["path"]})
def fs_read(path):
    return _rx_fs_call("read", path)


@tool("fs_write", "安全写入文件（≤1MB，需 __authorized=True）", "fs",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件路径"},
           "content": {"type": "string", "description": "内容"},
           "__authorized": {"type": "boolean", "description": "写操作授权（必须 true）"},
       },
        "required": ["path", "content"]},
      requires_auth=True)
def fs_write(path, content, __authorized=False):
    # requires_auth=True 在 registry.call 层强制校验（S86 决策：exe 永不自行放权）；
    # 此处保留 __authorized 形参仅为签名兼容。S90 写面收官：整体转调 rx-fs.exe——
    # 内容经 stdin 原始字节通道（argv 不传内容，绕开 Windows 命令行 32767 码元上限；
    # 二进制模式无换行翻译）。大小上限/沙盒 resolve/makedirs/tmp+replace 原子写全部
    # 在 exe 侧等价复刻（rust/src/fs.rs::op_write），顺序与旧实现一致（先大小后越界）。
    del __authorized
    content = content or ""
    return _rx_fs_call("write", path, stdin_bytes=content.encode("utf-8"))


@tool("fs_stat", "文件元信息（存在/大小/mtime）", "fs",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def fs_stat(path):
    return _rx_fs_call("stat", path)


@tool("fs_list", "列目录（深度可选，0=仅根层；Rust 原生）", "fs",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "目录"},
           "depth": {"type": "integer", "description": "递归深度（默认 1，上限 4）"},
       },
       "required": ["path"]})
def fs_list(path, depth=1):
    # S79 归正：旧实现 `depth or 1` 把字面 0 静默强制成 1；现 0 = 仅根层
    depth = 1 if depth is None else int(depth)
    return _rx_fs_call("list", path, depth)
