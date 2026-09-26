"""tools/cargosemver.py —— Rust API 兼容性检查薄壳（1 工具）：cargo_semver_checks

**cargo-semver-checks** 的能力探测薄壳（LIBRARY-POLICY：外部工具经能力探测
薄壳接入）。对比 git baseline 与当前工作区的公开 API，抓破坏性变更
（major）/ 需要新 minor 的增量。执行外部子进程（内部还要生成 rustdoc JSON，
较慢）⇒ requires_auth。

- 能力探测：shutil.which("cargo-semver-checks")——缺失如实报 + 安装 hint。
- 输入：Rust 项目目录（git 仓）+ baseline_rev（git tag / 分支 / rev）。
- 执行：`cargo-semver-checks --baseline-rev <rev>`（cwd=项目根）。
- 输出：failure 逐条（lint/标题）+ Summary 解析（required_bump = major/minor/
  none + 计数）；防御式解析，缺键如实置空。
- 诚实边界：只覆盖公开 API 的结构变化（rustdoc 可见的）；行为变化不归它管；
  rustdoc 生成需要本项目能编译（编译不过 = 如实报错）。

与 cargo_audit（RustSec 漏洞）/ cargo_machete（未使用依赖）同属 attack 域的
Rust 项目检查族：漏洞 / 依赖卫生 / API 兼容三件套。
"""
import os
import re
import shutil
import subprocess
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve

_FAILURE_RE = re.compile(r"^--- failure ([a-z0-9_]+): (.+?) ---$")
_SUMMARY_RE = re.compile(
    r"Summary semver requires new (major|minor) version: (\d+) major and (\d+) minor checks failed")


def _parse_semver_text(stdout):
    """纯函数：cargo-semver-checks 文本输出 → (failures, required_bump, major, minor)。

    failure 行：--- failure <lint>: <title> ---；Summary 行给出必需的版本跳档。
    行结构防御式解析：不认识的行跳过（上游改措辞不炸）。
    """
    failures = []
    required_bump = None
    major = minor = None
    for line in (stdout or "").splitlines():
        m = _FAILURE_RE.match(line.strip())
        if m:
            failures.append({"lint": m.group(1), "title": m.group(2)})
            continue
        m = _SUMMARY_RE.search(line)
        if m:
            required_bump = m.group(1)
            major, minor = int(m.group(2)), int(m.group(3))
    return failures, required_bump, major, minor


@tool("cargo_semver_checks",
      "Rust API 兼容性检查（cargo-semver-checks 薄壳，需授权）：对比 git baseline"
      " 与当前工作区的公开 API，逐条破坏性变更（lint/标题）+ Summary 判定需要"
      " major / minor 跳档；执行外部子进程并生成 rustdoc（较慢）。诚实边界：只看"
      " rustdoc 可见的公开 API 结构变化，行为变化不归它管；项目需能编译",
      "attack",
      {"type": "object",
       "properties": {
           "path": {"type": "string",
                    "description": "Rust 项目目录（git 仓，含 Cargo.toml）"},
           "baseline_rev": {"type": "string",
                            "description": "baseline 的 git tag / 分支 / rev（如 v0.1.0）"},
           "timeout": {"type": "integer", "description": "秒（默认 900，rustdoc 生成较慢）"}},
       "required": ["path", "baseline_rev"]},
      requires_auth=True)
def cargo_semver_checks(path, baseline_rev, timeout=900):
    t0 = time.time()
    exe = shutil.which("cargo-semver-checks")
    if exe is None:
        return {"ok": False, "available": False,
                "error": "cargo-semver-checks 未安装（外部 API 兼容检查器，能力探测未命中）",
                "hint": "cargo install cargo-semver-checks --locked"}
    if not re.fullmatch(r"[A-Za-z0-9._/\-]+", str(baseline_rev)):
        return {"error": "baseline_rev 含非法字符（只允许 git rev 的安全字符集）"}
    try:
        root = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root) or not os.path.isfile(os.path.join(root, "Cargo.toml")):
        return {"error": f"目录不存在或没有 Cargo.toml: {path}"}
    try:
        cp = subprocess.run(
            [exe, "--baseline-rev", str(baseline_rev)], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"cargo-semver-checks 超时（>{timeout}s）；rustdoc 生成较慢，"
                         "可加大 timeout"}
    # exit 0 = 无破坏性变更；1 = 有 failure；其余 = 真失败
    if cp.returncode not in (0, 1):
        return {"error": f"cargo-semver-checks 退出码 {cp.returncode}",
                "stderr": (cp.stderr or "")[:500]}
    failures, required_bump, major, minor = _parse_semver_text(cp.stdout)
    return {
        "ok": True,
        "root": root,
        "baseline_rev": baseline_rev,
        "failure_count": len(failures),
        "failures": failures,
        "required_bump": required_bump,
        "major_count": major,
        "minor_count": minor,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "note": ("只看 rustdoc 可见的公开 API 结构变化，行为变化不归它管；"
                 "项目需能编译（rustdoc 生成失败会如实报错）"),
    }
