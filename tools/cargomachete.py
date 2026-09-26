"""tools/cargomachete.py —— Rust 未使用依赖检测薄壳（1 工具）：cargo_machete

**cargo-machete** 的能力探测薄壳（LIBRARY-POLICY：外部工具经能力探测薄壳接入）。
执行外部子进程 ⇒ requires_auth（与 ide_build / cargo_audit 同款纪律）。

- 能力探测：shutil.which("cargo-machete")——缺失如实报 + 安装 hint。
- 输入：Rust 项目/工作区目录（含 Cargo.toml），路径过沙盒 _fs_resolve（S134）。
- 执行：`cargo-machete --format json`（cwd=项目根；workspace 自动递归）。
- 输出：逐条未使用依赖（package + 所在 Cargo.toml 路径）；干净如实报 0。
- 诚实边界：machete 走"字符串搜索 + 手写解析"而非编译器语义 ⇒ 宏里用的依赖
  可能误报（它自己文档承认）；检测结果只做线索，删依赖前人工确认。

与 cargo_audit（RustSec 漏洞）互补：那个管"已用依赖里的已知漏洞"，
这个管"根本没用的依赖"——依赖卫生的两半。
"""
import os
import re
import shutil
import subprocess
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve

_INSTALL_HINT = "cargo install cargo-machete --locked"


def _parse_machete_text(stdout):
    """纯函数：cargo-machete 文本输出 → 逐条未使用依赖。

    有发现的形状：
        cargo-machete found the following unused dependencies in this directory:
        <crate 名> -- <Cargo.toml 路径>:
        <TAB><依赖名>
    干净时是 "didn't find any unused dependencies"，自然解析出空表。
    行结构防御式解析：不认识的行跳过（上游改措辞不炸）。
    """
    out = []
    cur_manifest = None
    for line in (stdout or "").splitlines():
        m = re.match(r"^(.+?) -- (.+):$", line.strip())
        if m:
            cur_manifest = m.group(2)
            continue
        if line.startswith("\t") and cur_manifest:
            dep = line.strip()
            if dep:
                out.append({
                    "package": dep,
                    "manifest": cur_manifest,
                    "workspace_dir": os.path.dirname(cur_manifest) or None,
                })
    return out


@tool("cargo_machete",
      "Rust 未使用依赖检测（cargo-machete 薄壳，workspace 递归）：逐条多余的"
      "依赖（package + 所在 Cargo.toml）；执行外部子进程（需授权）。诚实边界："
      "字符串搜索非编译器语义——宏里用的依赖可能误报，删依赖前人工确认",
      "attack",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "Rust 项目/工作区目录（含 Cargo.toml）"},
           "timeout": {"type": "integer", "description": "秒（默认 300）"}},
       "required": ["path"]},
      requires_auth=True)
def cargo_machete(path, timeout=300):
    t0 = time.time()
    exe = shutil.which("cargo-machete")
    if exe is None:
        return {"ok": False, "available": False,
                "error": "cargo-machete 未安装（外部依赖卫生检测器，能力探测未命中）",
                "hint": _INSTALL_HINT}
    try:
        root = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root) or not os.path.isfile(os.path.join(root, "Cargo.toml")):
        return {"error": f"目录不存在或没有 Cargo.toml: {path}"}
    try:
        cp = subprocess.run([exe], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"cargo-machete 超时（>{timeout}s）"}
    if cp.returncode not in (0, 1):
        # 0 = 干净；1 = 有未使用依赖（不是工具失败）；2 = 真失败
        return {"error": f"cargo-machete 退出码 {cp.returncode}",
                "stderr": (cp.stderr or "")[:500]}
    items = _parse_machete_text(cp.stdout)
    return {
        "ok": True,
        "root": root,
        "count": len(items),
        "unnecessary_dependencies": items,
        "clean": not items,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "note": ("字符串搜索非编译器语义：宏里用的依赖可能误报，删依赖前人工确认"
                 "（cargo-machete 自身文档同款声明）"),
    }
