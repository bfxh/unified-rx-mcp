# -*- coding: utf-8 -*-
"""tools/fs.py —— 文件层（4 工具）：fs_read / fs_write / fs_stat / fs_list

S95 读面回迁：fs_read/fs_stat/fs_list 回到纯 Python——S94 EVAL §6 实锤，微秒级
操作（os.stat p50 0.008ms）走 exe 子进程要付 ~30ms 裸 spawn 溢价（三个数量级，
冷态更甚），这是路由决策的错不是 Rust 的错；回迁语义逐行对齐 rust/src/fs.rs，
golden master 锁等价（bench/s95_fs_golden.py 捕获 exe 臂 →
tests/test_s95_fs_back_contract.py 重放 40 场景全等）。fs_write 仍薄壳转调
rx-fs.exe（S90：内容经 stdin 字节通道 + tmp+replace 原子写；S86：exe 永不自行
放权，requires_auth 由 registry 统一强制）；exe 缺失/超时/非 JSON 输出 → 清晰
报错，不静默降级。

包络契约（回迁前后一致）：
- 沙盒拒绝（resolve 层）→ raise ValueError → registry 包成 ok:false；
- 工具级结果（不是文件/过大/不是目录）→ 返回 dict（error 走 result.error）。

安全设计（吸取旧版教训）：
- 沙盒：UNIFIED_RX_SANDBOX 环境变量（分号分隔多个根），Python/Rust 两侧同语义
- fail-closed：未设置 = 一律拒绝；"*" = 显式放开（自检/可信宿主用）
- 写保护：fs_write 需 __authorized=True（显式授权，防 AI 幻觉乱写）
- 大小上限：读 ≤1MB，写 ≤1MB
- 路径校验：解析绝对路径后校验沙盒前缀，realpath 防 symlink 出逃
"""
import json
import os
import stat
import subprocess
from pathlib import Path

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
    """薄壳转调 rx-fs.exe（S95 起仅 fs_write 在用），返回结果 dict；resolve 层
    拒绝 raise ValueError。

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
    # S95 回迁，语义 = rust/src/fs.rs op_read：先 stat 门（不存在/非常规文件同文）、
    # 再 1MB 门（size 为归一化前字节数）、utf-8/replace 解码 + universal newlines
    # （Path.read_text 默认 newline=None：\r\n 与 \r 都归一为 \n）；错误文本用原始入参。
    p = _resolve(path)
    try:
        md = os.stat(p)
    except OSError:
        return {"error": f"不是文件或不存在: {path}"}
    if not stat.S_ISREG(md.st_mode):
        return {"error": f"不是文件或不存在: {path}"}
    size = md.st_size
    if size > MAX_BYTES:
        return {"error": f"文件过大（{size} > {MAX_BYTES}），拒绝读取", "size": size}
    try:
        content = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ValueError(f"读取失败: {e}")
    return {"path": p, "size": size, "content": content}


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
    # S95 回迁，语义 = rust/src/fs.rs op_stat：幽灵路径返回 exists:false 而非报错；
    # mtime 秒级截断（int(st_mtime)：正数=floor 与 as_secs 同，负数=向零与 Rust
    # -(duration.as_secs()) 同）。
    p = _resolve(path)
    try:
        md = os.stat(p)
    except OSError:
        return {"exists": False, "path": p}
    return {"exists": True, "path": p,
            "is_file": stat.S_ISREG(md.st_mode),
            "is_dir": stat.S_ISDIR(md.st_mode),
            "size": md.st_size,
            "mtime": int(md.st_mtime)}


@tool("fs_list", "列目录（深度可选，0=仅根层）", "fs",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "目录"},
           "depth": {"type": "integer", "description": "递归深度（默认 1，上限 4）"},
       },
       "required": ["path"]})
def fs_list(path, depth=1):
    # S79 归正：旧实现 `depth or 1` 把字面 0 静默强制成 1；现 0 = 仅根层。
    # S95 回迁，语义 = rust/src/fs.rs op_list + walk：深度钳 0..=4；每层
    # sorted(listdir)（UTF-8 字节序 = 码点序）；OSError 层静默缺席；目录项无
    # size 键；文件 getsize 失败 = -1；rel 名用 os.sep。
    p = _resolve(path)
    if not os.path.isdir(p):
        return {"error": f"不是目录: {path}"}
    depth = max(0, min(4, int(depth)))
    entries = []

    def walk(d, cur):
        if cur > depth:
            return
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return  # 与 rust walk 同语义：该层静默缺席
        for name in names:
            full = os.path.join(d, name)
            rel = os.path.relpath(full, p)
            if os.path.isdir(full):
                entries.append({"name": rel, "type": "dir"})
                walk(full, cur + 1)
            else:
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = -1
                entries.append({"name": rel, "type": "file", "size": sz})

    walk(p, 0)
    return {"path": p, "total": len(entries), "entries": entries}
