"""依赖红线机器化（S147 立；本 PR **加严到供应链面**）。

CI（core.yml 依赖红线作业）与本地门（`local_gate.py` 的 deps-lock 步骤）各跑
一次——这些规矩改的是仓库里的文本文件，删一行、松一档都不会让任何测试变红，
所以必须由门盯住。三条判据：

- R1 **Rust 恒空**：`rust/Cargo.toml` 的 [dependencies] / [dev-dependencies] /
  [build-dependencies] / [target.'cfg(unix)'.dependencies] 有任一条目即红
  （与 Python 侧纯 stdlib 同纪律）。
- R2 **Python CI 依赖：登记 + 钉版为默认**：`.github/ci-requirements.txt` 每行
  的包名必须在 `spec/LIBRARY-POLICY.md` §六「外部组件清单」登记（留理由），且必须
  `==` 钉版；**浮动是例外，须同行留书面理由**（`# 浮动：理由见 LIBRARY-POLICY
  §六①`）——钉版默认、浮动留痕。
- R3 **workflow 钉死或留痕**：`.github/workflows/*.yml` 的 `uses:` 必须钉 40 位
  hex sha；钉不了的（如 `dtolnay/rust-toolchain` 的 ref 就是工具链名，无 sha 语义）
  必须写 `zizmor: ignore[unpinned-uses] <理由>`，理由不许为空。

为什么这样加严：CI/CD 上的依赖是**供应链攻击面**——浮动 tag 指到哪算哪，未登记的
库等于没人过三问。门的意义不是"不许用"，而是"用要留痕、理由可查"。
想新引依赖或外接组件，先过 `spec/LIBRARY-POLICY.md`：三问（理念契合 / 版本前沿 /
token 账）+ 第 4 问「90 天维护线」+ §二 的"能力探测薄壳"合法形态。

用法：python -X utf8 scripts/deps_lock.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARGO = ROOT / "rust" / "Cargo.toml"
REQ = ROOT / ".github" / "ci-requirements.txt"
POLICY = ROOT / "spec" / "LIBRARY-POLICY.md"
WF = ROOT / ".github" / "workflows"
SECTIONS = ("[dependencies]", "[dev-dependencies]", "[build-dependencies]",
            "[target.'cfg(unix)'.dependencies]")
FLOAT_MARK = "# 浮动：理由见 LIBRARY-POLICY"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"uses:\s*\S+@(\S+)")
NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def offenders(text):
    out, cur = [], None
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line
        elif cur in SECTIONS and line and not line.startswith("#"):
            out.append(f"L{ln}: {cur} {line[:60]}")
    return out


def req_offenders(policy, lines):
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = (NAME_RE.match(line) or [line, line])[1]
        if name not in policy or ("==" not in line and FLOAT_MARK not in raw):
            out.append(f"{name}：未登记 或 未钉版且无浮动理由（默认必须 `==`）")
    return out


def _unpinned_ok(tail):
    return "zizmor: ignore[unpinned-uses]" in tail and tail.split("]", 1)[-1].strip()


def workflow_offenders(text):
    out = []
    for line in text.splitlines():
        m = USES_RE.search(line)
        if m and not SHA_RE.match(m.group(1)) and not _unpinned_ok(line[m.end():]):
            out.append(f"{line.strip()[:70]}：未钉 sha 且无 zizmor 例外理由")
    return out


def collect():
    fails = [f"rust/Cargo.toml {x}" for x in offenders(CARGO.read_text("utf-8"))]
    policy = POLICY.read_text(encoding="utf-8")
    fails += [f"ci-requirements {x}" for x
              in req_offenders(policy, REQ.read_text("utf-8").splitlines())]
    for p in sorted(WF.glob("*.yml")):
        fails += [f"{p.name} {x}" for x in workflow_offenders(p.read_text("utf-8"))]
    return fails


def main():
    if not CARGO.is_file():
        sys.exit(f"DEPS-LOCK FAIL: 缺 {CARGO}")
    fails = collect()
    print("DEPS-LOCK 违规=" + str(len(fails)))
    if fails:
        sys.exit("\n  ".join(["DEPS-LOCK FAIL: 依赖/供应链红线被破", *fails]))
    print("DEPS-LOCK OK")


if __name__ == "__main__":
    main()
