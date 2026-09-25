"""路径门（S162）：提交路径上的**路径纪律**——与 key 门（明文红线）并列为自提交 PR 的双门。

为什么要有它：本仓是"80 个工具在大量目录上读写"的项目，路径是最容易出逃逸面的一环
（符号链接指向仓库外、`..` 拼接写到沙盒/仓库之外、大二进制混入、文件名带设备名或控制字符）。
这些**不会**被明文门抓（不是密钥）、也**不会**被测试抓（测试不查仓库卫生）。

判据（全部机器可判；命中即红，除非在 `spec/path-exempt.json` 里带 `why` 登记）：
  P1 无符号链接（mode 120000）——指向外部的逃逸面；
  P2 文件名卫生：不得含 `..` / 绝对路径形态（`C:`、前导 `/`）/ Windows 设备名
     （CON/PRN/AUX/NUL/COM[1-9]/LPT[1-9]）/ 结尾空格或点 / 控制字符；
  P3 单文件 ≤ 1MB（大二进制/数据混入仓库的典型信号）；
  P4 源码里不得出现「`..` 与写文件原语同现」的越界写路径（启发式，可豁免登记）；
  P5 工作树里不得有指向仓库外的软链接（realpath 逃逸）。

用法：python scripts/path_gate.py   （退出码 0 = 通过；1 = 命中）
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT = ROOT / "spec" / "path-exempt.json"
MAX_BYTES = 1024 * 1024
DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
# P4：一行里同时出现 `..` 字面量与写/拷贝原语 ⇒ 可疑越界写
DANGEROUS = re.compile(r"""(["']\.\.["']|\.\./|\.\.\\\\)""")
WRITE_PRIM = re.compile(r"(open\(|write_text\(|write_bytes\(|os\.path\.join|shutil\.copy|cp\s|Path\()")
SRC_EXT = (".py", ".sh", ".rs", ".yml", ".yaml", ".toml")


def _git(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          shell=False).stdout


def load_exempt():
    if not EXEMPT.is_file():
        return []
    doc = json.loads(EXEMPT.read_text(encoding="utf-8"))
    out = []
    for e in doc.get("entries") or []:
        if not e.get("why"):
            sys.exit(f"PATH-GATE FAIL: 豁免条目缺 why（{e}）——豁免必须写明理由")
        out.append(e)
    return out


def exempt(entries, path, rule):
    return any(e.get("rule") == rule and e.get("path") == path for e in entries)


def main() -> int:
    fails, ex = [], load_exempt()
    files = [f for f in _git("ls-files").splitlines() if f.strip()]

    # P1 符号链接（git 索引里 mode=120000）
    for line in _git("ls-files", "-s").splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[0] == "120000":
            fails.append(f"P1 符号链接：{parts[3]}（指向外部的逃逸面）")

    for f in files:
        p = ROOT / f
        # P2 文件名卫生
        base = os.path.basename(f)
        bad = []
        if ".." in f.split("/") or ".." in f.split("\\"):
            bad.append("含 `..`")
        if re.match(r"^[A-Za-z]:", f) or f.startswith("/"):
            bad.append("绝对路径形态")
        if DEVICE.match(base):
            bad.append("Windows 设备名")
        if base.rstrip() != base or base.endswith("."):
            bad.append("结尾空格/点")
        if any(ord(c) < 32 for c in f):
            bad.append("控制字符")
        if bad and not exempt(ex, f, "P2"):
            fails.append(f"P2 文件名不卫生：{f}（{'、'.join(bad)}）")
        # P3 体积
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > MAX_BYTES and not exempt(ex, f, "P3"):
            fails.append(f"P3 超大文件：{f}（{size/1e6:.1f}MB > 1MB）")
        # P4 越界写路径（仅源码后缀）
        if p.suffix in SRC_EXT and not exempt(ex, f, "P4"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if DANGEROUS.search(line) and WRITE_PRIM.search(line):
                    fails.append(f"P4 可疑越界写路径：{f}:{i} → {line.strip()[:80]}")

    # P5 工作树里指向仓库外的软链接
    real_root = os.path.realpath(ROOT)
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d != ".git" and not d.startswith(".urx-hist")]
        for name in filenames + dirnames:
            q = os.path.join(dirpath, name)
            if os.path.islink(q):
                tgt = os.path.realpath(q)
                if not tgt.startswith(real_root + os.sep):
                    fails.append(f"P5 软链接逃逸：{os.path.relpath(q, ROOT)} → {tgt}")

    if fails:
        for f in fails[:20]:
            print(f"  ❌ {f}")
        print(f"PATH-GATE FAIL: {len(fails)} 处（豁免登记见 spec/path-exempt.json）")
        return 1
    print(f"PATH-GATE OK（P1 无符号链接 / P2 文件名卫生 / P3 ≤1MB / P4 无越界写 / P5 无软链接逃逸；"
          f"{len(files)} 个跟踪文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
