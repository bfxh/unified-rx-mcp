"""scripts/typos_gate.py —— 拼写门（S171）：typos **零容忍**（豁免进 `_typos.toml`）。

判据：`typos .` 退出码 0（无拼写命中）。豁免两类，都必须写理由（配置在仓内 `_typos.toml`）：
① **生成物排除**（bench/results / manual_snaps——语料里的代码原文不是拼写错误）；
② **域名术语自映射**（ACI / ND / clen / ba——OpenCL API 与内核形参、本仓概念缩写）。

**工具缺失不静默**：typos 不在 PATH 直接 FAIL（CI 由 `ci-requirements.txt` 钉版装，
本机用 /c/vxl-wl-tools/typos.exe 或 pip 装 typos）。

用法：
  python -X utf8 scripts/typos_gate.py                # 判红（默认 root=.）
  python -X utf8 scripts/typos_gate.py --list         # 逐条打印命中
  python -X utf8 scripts/typos_gate.py --root <r>     # 自定义根（金丝雀用）
"""
import argparse
import shutil
import subprocess


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    exe = shutil.which("typos")
    if not exe:
        print("FAIL typos-gate typos 不可用（PATH 里没有）——"
              "本地 `pip install typos==1.50.2`，CI 由 .github/ci-requirements.txt 安装"
              "（缺工具不静默降级）")
        return 1
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout.strip()

    p = subprocess.run([exe, "--format", "brief", "."], cwd=a.root,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", shell=False)
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]

    print(f"TYPOS-GATE root={a.root} ({ver}) 命中={len(lines)}"
          f"（豁免/排除见 _typos.toml，零容忍）")
    if a.list or lines:
        for ln in lines[:30]:
            print(f"  ✗ {ln}")
        if len(lines) > 30:
            print(f"  …（共 {len(lines)} 条）")
    if p.returncode == 0:
        print("TYPOS-GATE OK 0 命中")
        return 0
    if p.returncode in (1, 2):      # 实测（金丝雀单命中）：2 = 有拼写命中
        print(f"TYPOS-GATE FAIL 拼写命中={len(lines)}（真错就改，域名术语/夹具词进 _typos.toml 自映射）")
    else:
        print(f"TYPOS-GATE FAIL typos 异常退出 {p.returncode}：{(p.stderr or '')[:200]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
