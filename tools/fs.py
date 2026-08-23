# -*- coding: utf-8 -*-
"""tools/fs.py —— 文件层（4 工具）：fs_read / fs_write / fs_stat / fs_list

安全设计（吸取旧版教训）：
- 沙盒：UNIFIED_RX_SANDBOX 环境变量（分号分隔多个根）；未设置 = 不限制
- 写保护：fs_write 需 __authorized=True（显式授权，防 AI 幻觉乱写）
- 大小上限：读 ≤1MB，写 ≤1MB
- 路径校验：解析绝对路径后校验沙盒前缀（防 ../ 逃逸）
"""
import os

from registry import tool

SANDBOX_ENV = "UNIFIED_RX_SANDBOX"
MAX_BYTES = 1_000_000


def _sandbox_roots():
    env = os.environ.get(SANDBOX_ENV, "")
    if not env:
        return None  # 未设置 = 不限制
    return [os.path.abspath(p) for p in env.split(";") if p]


def _in_sandbox(path):
    roots = _sandbox_roots()
    if roots is None:
        return True
    ap = os.path.abspath(path)
    for r in roots:
        if ap == r or ap.startswith(r + os.sep):
            return True
    return False


def _resolve(path):
    """校验沙盒 + 返回绝对路径。越界抛 ValueError。"""
    if not isinstance(path, str) or not path:
        raise ValueError("path 必填")
    ap = os.path.abspath(path)
    if not _in_sandbox(ap):
        raise ValueError(f"路径越界（沙盒外）: {path}")
    return ap


@tool("fs_read", "安全读取文件（≤1MB，沙盒校验）", "fs",
      {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}},
       "required": ["path"]})
def fs_read(path):
    p = _resolve(path)
    if not os.path.isfile(p):
        return {"error": f"不是文件或不存在: {path}"}
    size = os.path.getsize(p)
    if size > MAX_BYTES:
        return {"error": f"文件过大（{size} > {MAX_BYTES}），拒绝读取", "size": size}
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"path": p, "size": size, "content": content}


@tool("fs_write", "安全写入文件（≤1MB，需 __authorized=True）", "fs",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件路径"},
           "content": {"type": "string", "description": "内容"},
           "__authorized": {"type": "boolean", "description": "写操作授权（必须 true）"},
       },
       "required": ["path", "content"]})
def fs_write(path, content, __authorized=False):
    if not __authorized:
        raise PermissionError("写操作需要授权：参数加 __authorized: true 确认后重试")
    if len(content or "") > MAX_BYTES:
        return {"error": f"内容过大（{len(content)} > {MAX_BYTES}）"}
    p = _resolve(path)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return {"error": f"创建目录失败: {e}"}
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return {"path": p, "size": len(content), "ok": True}


@tool("fs_stat", "文件元信息（存在/大小/mtime）", "fs",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def fs_stat(path):
    p = _resolve(path)
    if not os.path.exists(p):
        return {"exists": False, "path": p}
    st = os.stat(p)
    return {
        "exists": True,
        "path": p,
        "is_file": os.path.isfile(p),
        "is_dir": os.path.isdir(p),
        "size": st.st_size,
        "mtime": int(st.st_mtime),
    }


@tool("fs_list", "列目录（≤200 项，深度可选）", "fs",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "目录"},
           "depth": {"type": "integer", "description": "递归深度（默认 1）"},
       },
       "required": ["path"]})
def fs_list(path, depth=1):
    p = _resolve(path)
    if not os.path.isdir(p):
        return {"error": f"不是目录: {path}"}
    depth = max(0, min(int(depth or 1), 4))
    entries = []

    def walk(d, cur_depth):
        if cur_depth > depth:
            return
        try:
            items = sorted(os.listdir(d))
        except OSError:
            return
        for it in items:
            full = os.path.join(d, it)
            rel = os.path.relpath(full, p)
            if os.path.isdir(full):
                entries.append({"name": rel, "type": "dir"})
                walk(full, cur_depth + 1)
            else:
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = -1
                entries.append({"name": rel, "type": "file", "size": sz})

    walk(p, 0)
    return {"path": p, "total": len(entries), "entries": entries[:200]}
