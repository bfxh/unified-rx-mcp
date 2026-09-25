"""scripts/quality_pact_gate.py —— 质量-速度契约门（S173，CD-PLATFORM §三.2）。

**判据**：范围内主题以 `perf` 开头的提交（`perf:` / `perf(xxx):`），正文必须含一行
`EVIDENCE:`——三选一（对应 CD-PLATFORM §3.1）：
  · `EVIDENCE: 对拍一致`（+哈希/口径）——行为不变的性能改动；
  · `EVIDENCE: 行为变更+理由`——性能改动同时是产品口径变化（须走金样更新/理由登记）；
  · `EVIDENCE: 基础设施`——门/CI/工具自身的性能改动（金丝雀另证）。
非 perf 提交不受约束。**提速不得是质量的债务转移**——这条就是它的机器化。

范围自动判定：`--range` 显式 > `origin/main..HEAD`（可解析时）> `HEAD~1..HEAD`。
git 缺失/范围解析失败 **FAIL 不静默**。

用法：
  python -X utf8 scripts/quality_pact_gate.py                 # 自动范围
  python -X utf8 scripts/quality_pact_gate.py --range A..B    # 显式范围
"""
import subprocess
import sys


_ROOT = "."


def _git(*args: str) -> str:
    p = subprocess.run(["git", "-C", _ROOT, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{(p.stderr or '')[:200]}")
    return p.stdout


def auto_range() -> str:
    """origin/main..HEAD 可解析（有差异或恰好无差异）则用之；否则退回 HEAD~1..HEAD。"""
    try:
        out = _git("rev-parse", "--verify", "--quiet", "origin/main").strip()
        if out:
            return "origin/main..HEAD"
    except RuntimeError:
        pass
    return "HEAD~1..HEAD"


def perf_commits_without_evidence(commit_lines: list[str]) -> list[str]:
    """纯函数（金丝雀直接测）：commit_lines 交替 (subject, body)；返回违规 subject。"""
    bad = []
    for i in range(0, len(commit_lines) - 1, 2):
        subject, body = commit_lines[i], commit_lines[i + 1]
        is_perf = subject.startswith(("perf:", "perf("))
        has_ev = any(ln.startswith("EVIDENCE:") for ln in body.splitlines())
        if is_perf and not has_ev:
            bad.append(subject)
    return bad


def main() -> int:
    argv = sys.argv[1:]
    rng = None
    global _ROOT
    if "--root" in argv:
        i = argv.index("--root")
        _ROOT = argv[i + 1]
    if "--range" in argv:
        i = argv.index("--range")
        rng = argv[i + 1] if i + 1 < len(argv) else None
        if not rng:
            print("FAIL quality-pact --range 需要一个范围值")
            return 1
    try:
        rng = rng or auto_range()
        raw = _git("log", "--format=%s%n%b%n---CUT---", rng)
    except RuntimeError as e:
        print(f"FAIL quality-pact {e}")
        return 1

    # 解析：每条提交 = subject 行 + body（多行），---CUT--- 分隔
    commit_pairs: list[str] = []
    cur_subj: str | None = None
    cur_body: list[str] = []
    for ln in raw.splitlines():
        if ln == "---CUT---":
            if cur_subj is not None:
                commit_pairs.extend([cur_subj, "\n".join(cur_body)])
            cur_subj = None
            cur_body = []
            continue
        if cur_subj is None and ln.strip():
            cur_subj = ln
        elif cur_subj is not None:
            cur_body.append(ln)
    if cur_subj is not None:
        commit_pairs.extend([cur_subj, "\n".join(cur_body)])

    bad = perf_commits_without_evidence(commit_pairs)
    n_perf = sum(1 for i in range(0, len(commit_pairs) - 1, 2)
                 if commit_pairs[i].startswith(("perf:", "perf(")))
    print(f"QUALITY-PACT 范围={rng} 提交={len(commit_pairs) // 2} perf={n_perf}")
    if bad:
        for s in bad:
            print(f"  ✗ {s}")
        print("QUALITY-PACT FAIL perf 提交缺 EVIDENCE 行"
              "（对拍一致 / 行为变更+理由 / 基础设施 三选一，见 spec/CD-PLATFORM.md §三）")
        return 1
    print("QUALITY-PACT OK 全部 perf 提交带 EVIDENCE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
