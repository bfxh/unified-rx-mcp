"""tools/cargoaudit.py —— Rust 依赖安全审计薄壳（1 工具）：cargo_audit

RustSec 官方 advisory 库经 **cargo-audit** 的能力探测薄壳（LIBRARY-POLICY：
外部工具经能力探测薄壳接入、单点接开源最强）。执行外部子进程 ⇒ requires_auth
（与 ide_build / ide_diag 的 clippy 透镜同款纪律，registry 层统一把关）。

- 能力探测：shutil.which("cargo-audit")——缺失**如实报**（不静默、不假绿），
  hint 给安装命令；装好后本工具自动可用，零接线。
- 输入：Rust 项目目录（须含 Cargo.lock），路径过沙盒 _fs_resolve（S134）。
- 执行：`cargo audit --json -f <Cargo.lock>`（cwd=项目根；advisory DB 由
  cargo-audit 自管更新，离线环境可传 stale=True 跳过更新）。
- 输出：vulnerabilities 逐条（id/package/title/patched）+ warnings 摘要 +
  计数；列表截断交 registry 出口上限，另有本工具 50 条自帽。
- 诚实边界：只覆盖 Cargo.lock 里的依赖树（cargo audit 对无 lock 的项目要求
  先 generate-lockfile）；advisory DB 时效由上游维护；本工具不做修复，
  修复动作（升级/换 crate）是人的决策。

与 secrets_hunt / installer_scan 互补：那两个管源码凭据与安装包伪造，
这个管 Rust 依赖树里的已知漏洞（RustSec）。
"""
import json
import os
import shutil
import subprocess
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve

_MAX_LIST = 50

_INSTALL_HINT = "cargo install cargo-audit --locked"


def _summarize(payload, max_list):
    """纯函数：cargo-audit --json 的 payload → 摘要结构。

    字段全部防御式读取：cargo-audit 各版本的 JSON 形状有小差异（0.17/0.18+
    的 warnings 结构不同），解析器只抽共识字段，缺如实置空。
    """
    vulns_block = payload.get("vulnerabilities") or {}
    vuln_list = [{
        "id": item.get("id") or "",
        "package": (item.get("package") or {}).get("name", ""),
        "version": (item.get("package") or {}).get("version", ""),
        "title": item.get("title") or "",
        "patched": item.get("patched") or [],
    } for item in (vulns_block.get("list") or [])[:max_list]]
    warnings_raw = payload.get("warnings") or {}
    warnings = {}
    for kind, items in warnings_raw.items():
        names = []
        for w in items[:_MAX_LIST]:
            pkg = (w.get("package") or {}).get("name", "")
            if pkg:
                names.append(pkg)
        if names:
            warnings[kind] = {"count": len(items), "packages": names}
    return {
        "vulnerability_count": vulns_block.get("count",
                                               len(vulns_block.get("list") or [])),
        "vulnerabilities": vuln_list,
        "warnings": warnings,
    }


@tool("cargo_audit",
      "Rust 依赖安全审计（RustSec advisory 薄壳，经 cargo-audit --json）：逐条"
      "漏洞（id/package/版本/title/patched 版本）+ unmaintained 等警告摘要；"
      "执行外部子进程（需授权）。诚实边界：只覆盖 Cargo.lock 依赖树；"
      "advisory DB 时效由上游管；本工具不做修复，升级/换 crate 是人的决策",
      "attack",
      {"type": "object",
       "properties": {
           "path": {"type": "string",
                    "description": "Rust 项目目录（须含 Cargo.lock）"},
           "stale": {"type": "boolean",
                     "description": "true=跳过 advisory DB 更新（离线环境）"},
           "max_list": {"type": "integer",
                        "description": "返回漏洞条数上限（默认 50）"},
           "timeout": {"type": "integer", "description": "秒（默认 300）"}},
       "required": ["path"]},
      requires_auth=True)
def cargo_audit(path, stale=False, max_list=50, timeout=300):
    t0 = time.time()
    exe = shutil.which("cargo-audit")
    if exe is None:
        return {"ok": False, "available": False,
                "error": "cargo-audit 未安装（外部依赖审计器，能力探测未命中）",
                "hint": _INSTALL_HINT}
    try:
        root = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {path}"}
    if not os.path.isfile(os.path.join(root, "Cargo.lock")):
        return {"error": ("项目根没有 Cargo.lock——先 `cargo generate-lockfile`；"
                          "纯 Cargo.toml 项目没有审计对象")}
    max_list = max(1, min(int(max_list), 200))
    cmd = [exe, "audit", "--json", "-f", os.path.join(root, "Cargo.lock")]
    if stale:
        cmd.append("--stale")
    try:
        cp = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"cargo-audit 超时（>{timeout}s）；advisory DB 首次克隆可能较慢，"
                         "可重试或 stale=true"}
    # exit 1 = 发现漏洞（不是工具失败）；其他非零 = 真失败，如实转述 stderr
    if cp.returncode not in (0, 1):
        return {"error": f"cargo-audit 退出码 {cp.returncode}",
                "stderr": (cp.stderr or "")[:500]}
    try:
        payload = json.loads(cp.stdout or "{}")
    except ValueError:
        return {"error": "cargo-audit 输出不是合法 JSON（版本不兼容？）",
                "stdout_head": (cp.stdout or "")[:300]}
    out = _summarize(payload, max_list)
    out.update({
        "ok": True,
        "root": root,
        "exit_code": cp.returncode,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "note": ("静态审计非保证：只覆盖 Cargo.lock 依赖树，DB 时效由上游管；"
                 "修复（升级/换 crate）是人的决策"),
    })
    return out
