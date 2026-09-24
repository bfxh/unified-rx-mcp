"""scripts/gitleaks_gate.py —— 凭据泄漏门·业界规则库（S172）：gitleaks **零容忍**。

与自家 secrets 门的关系：自家门管"本仓口径"（key 门/历史 diff/块过滤），gitleaks 补
**业界规则库**（generic-api-key 熵规则 + 100+ 提供商规则）——互补不重复。
豁免在仓内 `.gitleaks.toml`（夹具假凭据/编译产物/审计文档样本，每条写理由）。

**本地优先**：gitleaks 是 Go 单二进制——本机 `/c/vxl-wl-tools/gitleaks.exe`（或 PATH，
或 `UNIFIED_RX_GITLEAKS` 指定）；CI 按钉版 URL 下载同一版本。工具缺失 **FAIL 不静默**。

用法：
  python -X utf8 scripts/gitleaks_gate.py            # 判红（dir 扫描，含未跟踪文件）
  python -X utf8 scripts/gitleaks_gate.py --git      # 加 git 历史（周扫用）
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
_FALLBACK = r"C:\vxl-wl-tools\gitleaks.exe"


def _exe() -> str:
    exe = os.environ.get("UNIFIED_RX_GITLEAKS") or shutil.which("gitleaks")
    if exe:
        return exe
    if os.path.isfile(_FALLBACK):
        return _FALLBACK
    raise RuntimeError(
        "gitleaks 不可用——本地放 /c/vxl-wl-tools/gitleaks.exe（或 PATH / UNIFIED_RX_GITLEAKS），"
        "CI 按钉版 URL 下载 v8.30.1（缺工具不静默降级）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="扫描根（金丝雀用临时根）")
    ap.add_argument("--git", action="store_true", help="附加 git 历史扫描（周扫）")
    a = ap.parse_args()
    root = str(pathlib.Path(a.root).resolve())
    try:
        exe = _exe()
    except RuntimeError as e:
        print(f"FAIL gitleaks-gate {e}")
        return 1
    ver = subprocess.run([exe, "version"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout.strip()

    modes = ["dir"] + (["git"] if a.git else [])
    hits = []
    for mode in modes:
        report = os.path.join(os.environ.get("TEMP", "."), f"gl-{mode}.json")
        p = subprocess.run([exe, mode, root, "--report-path", report,
                            "--report-format", "json", "--no-banner",
                            "--config", ".gitleaks.toml"],
                           cwd=str(REPO), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", shell=False,
                           timeout=1800)
        if p.returncode not in (0, 1):
            print(f"FAIL gitleaks-gate {mode} 异常 rc={p.returncode}：{(p.stderr or '')[-200:]}")
            return 1
        try:
            hits.extend(json.loads(pathlib.Path(report).read_text(encoding="utf-8")))
            pathlib.Path(report).unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"FAIL gitleaks-gate 报告读不了：{e}")
            return 1

    print(f"GITLEAKS-GATE ({ver}) 命中={len(hits)}（模式：{'+'.join(modes)}；"
          f"夹具豁免见 .gitleaks.toml，零容忍）")
    if hits:
        for x in hits[:20]:
            f = str(x.get("File", "?")).replace("\\", "/")
            print(f"  ✗ {f}:{x.get('StartLine')} {x.get('RuleID')}")
        print(f"GITLEAKS-GATE FAIL 命中={len(hits)}（真泄漏就处理；"
              f"夹具假凭据走 .gitleaks.toml 豁免并写理由）")
        return 1
    print("GITLEAKS-GATE OK 0 命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
