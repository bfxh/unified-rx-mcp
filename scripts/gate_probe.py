r"""门是真门吗？——给每一道门注入一次最小违规，它**必须**转红。

为什么要有它：本仓 27 道门，门越多越可能有失效的那几道，而失效的门**不会让任何测试变红**、
CI 还挂着绿勾。三类成因：

  1. **判据写错**：一条正则想一次匹配完「结构 + 内容」时可选组会贪婪吃掉整个字符串
     ⇒ 永远匹配不上（姊妹仓 qingjian 的 `ident_gate` 栽过这个）。
  2. **扫描面没覆盖到**：os.walk 的 `dirnames[:]` 过滤把目标目录剪掉、EXCLUDE_PREFIX 写宽、
     相对路径 vs 带前导斜杠的片段 ⇒ 门看着在跑，其实扫的是空集。
  3. **注入本身不够像**（本文件的踩坑）：探针只有 0.72MB 而阈值是 1MB、GitHub token 少 4 位、
     `str.replace` 改到注释里的 `[dependencies]` ⇒ 得到**假阴性**，会冤枉好门。

判据：注入前门是绿的、注入后红了 ⇒ 真门。注入前就红（本机工具缺失等）标 `无法验证`，
**不冒充通过**。

**本文件自己要过自己**：它含凭据形状与错拼词，会被 secrets / gitleaks / typos / lint 扫到，
所以这些探针值一律**运行时生成**，源码里不留完整字面量。

用法：python -X utf8 scripts/gate_probe.py [--only god-gate,…]
退出码：0 = 能验的门都能被触发；1 = 有门注入了也不红（空门/判据失效）。
"""
import json
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "_probe"
PY = sys.executable

# 需要 git 索引的门（path_gate 用 `git ls-files`）：注入前 git add，跑完撤回来。
GIT_INDEXED = {"path-gate"}


def _rand(n, alphabet=None):
    import random
    return "".join(random.choices(alphabet or (string.ascii_letters + string.digits), k=n))


def _aws_creds():
    """运行时拼出 AWS 假凭据形状——源码里不留字面量，否则本文件先被 secrets 门抓到。"""
    akia = "AK" + "IA" + _rand(16, string.ascii_uppercase + string.digits)
    sec = _rand(40)
    return f'AWS_KEY = "{akia}"\nAWS_SEC = "{sec}"\n'


def _gh_token():
    """ghp_ + 36 字符（gitleaks 的 GitHub token 规则要满 36 位）。"""
    return 'TOKEN = "' + "gh" + "p_" + _rand(36) + '"\n'


def _typo_text():
    # 错拼词运行时拼：写成字面量会让 typos 门先抓本文件（第一次跑就现形了）。
    return 'MSG = "' + "rec" + "ieve the " + "pak" + 'age"\n'


def _escape_text():
    """P4 越界写探针：`..` 与 `open(` 都**运行时拼**——写成字面量会让 path-gate
    先抓本文件（同一行里两者同现即命中），那才是真正的假阴性来源。"""
    dots = chr(46) + chr(46)
    return "p = " + "op" + "en(" + 'os.path.join("' + dots + '", "outside.txt"), "w")\n'


# (门名, 脚本, {文件名: 内容或生成函数})
PROBES = [
    ("god-gate", "scripts/god_gate.py", {"big.py": lambda: "x = 1\n" * 850}),
    ("lint-gate", "scripts/lint_gate.py", {"lint.py": lambda: "import os\n"}),   # F401
    # mypy 默认档会报 "Incompatible return value"；本机 mypy 缺 ⇒ 自动标"注入前已红"。
    ("type-gate", "scripts/type_gate.py",
     {"bad_type.py": lambda: "def f(x: int) -> str:\n    return x\n"}),
    ("typos-gate", "scripts/typos_gate.py", {"typo.py": _typo_text}),
    ("secrets", "scripts/ci_secrets_gate.py", {"secret.py": _aws_creds}),
    ("gitleaks-gate", "scripts/gitleaks_gate.py", {"leak.py": _gh_token}),
    # path-gate P3 是 >1MB（`y = 2\n`×120000 只有 0.72MB ⇒ 假阴性）；改用 P4 越界写：
    # 同一行里 `..` 与写文件原语同现即命中。
    ("path-gate", "scripts/path_gate.py", {"escape.py": _escape_text}),
]

# 复制一份现有源码（雷同门）
COPIES = [("dupe-gate", "scripts/dupe_gate.py", "tools/attack.py")]


def _add_dep(text):
    # 只替换**行首**的 `[dependencies]`：Cargo.toml 注释里也写着这三个字，str.replace
    # 会改到注释那一行，而注释行被判据跳过 ⇒ 假阴性。
    return re.sub(r"^\[dependencies\]$", '[dependencies]\nserde = "1"', text,
                  count=1, flags=re.MULTILINE)


def _break_ledger(text):
    doc = json.loads(text)
    entries = doc if isinstance(doc, list) else doc.get("entries", [])
    if entries:
        entries[0]["seal"] = "not-a-sha256-prefix"
    return json.dumps(doc, ensure_ascii=False, indent=2)


MUTATIONS = [
    ("deps-lock", "scripts/deps_lock.py", "rust/Cargo.toml", _add_dep),
    ("audit-freshness", "scripts/audit_ledger.py", "spec/audit-ledger.json", _break_ledger),
]

# 本机跑不了 / 依赖独占机器 / 依赖外部工具 ⇒ 明确标出，不冒充通过
CANNOT_VERIFY = {
    # 下面几道是"还没写探针"，不是"门有问题"——写探针要造协议违约/新提交/改金样，代价大。
    # 明确列出来是为了不拿沉默冒充通过。
    "mcp-surface": "待补探针（需造协议违约回包）",
    "tool-evals": "待补探针（需造任务评测偏差）",
    "perf-gate": "计时档：需独占机器（UNIFIED_RX_TIMING_GATES=1）",
    "cli-bench": "计时档：需独占机器（UNIFIED_RX_TIMING_GATES=1）",
    "coverage-gate": "覆盖率档：本机无 profiler runtime（UNIFIED_RX_COVERAGE_GATES=1）",
    "pytest": "全量测试档（非 fast）",
    "stress": "高压档（非 fast）",
    "cargo-test": "需 Rust 工具链（非 fast）",
    "clippy": "需 Rust 工具链（非 fast）",
    "self-attack": "巡航档：注入需改 attack 语料",
    "data-flow": "taint 档：注入需改 taint 基线",
    "secrets-history": "历史 diff 档：注入需造提交",
    "toolface": "需注册新工具才触发体量软帽",
    "model-fit": "需注册新工具才触发回包预算",
    "cli-golden": "金标准比对：注入需改金样文件",
    "quality-pact": "需造一个 perf 提交",
    "selftest": "对账硬门：注入需改 SCHEMA/EXE/VERSION",
}


def run(script):
    cp = subprocess.run([PY, "-X", "utf8", script], cwd=str(ROOT), capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=1800)
    return cp.returncode


def git(*args):
    subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def write_probe(files):
    PROBE.mkdir(parents=True, exist_ok=True)
    for fn, text in files.items():
        (PROBE / fn).write_text(text() if callable(text) else text, encoding="utf-8")


def clear_probe(name):
    if name in GIT_INDEXED:
        git("rm", "-r", "--cached", "-q", str(PROBE))
    shutil.rmtree(PROBE, ignore_errors=True)


def check(name, script, before, rows, bad, unsure):
    after = run(script)
    rows.append((name, before, after))
    if after == 0:
        bad.append(name)
    elif before != 0:
        unsure.append(name)


def probe_files(only, rows, bad, unsure):
    for name, script, files in PROBES:
        if only and name not in only:
            continue
        before = run(script)
        write_probe(files)
        if name in GIT_INDEXED:
            git("add", str(PROBE))
        check(name, script, before, rows, bad, unsure)
        clear_probe(name)


def probe_copies(only, rows, bad, unsure):
    for name, script, src in COPIES:
        if only and name not in only:
            continue
        before = run(script)
        PROBE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, PROBE / (Path(src).stem + "_copy.py"))
        check(name, script, before, rows, bad, unsure)
        clear_probe(name)


def probe_mutations(only, rows, bad, unsure):
    for name, script, rel, mutate in MUTATIONS:
        if only and name not in only:
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        original = p.read_text(encoding="utf-8")
        before = run(script)
        p.write_text(mutate(original), encoding="utf-8")
        try:
            check(name, script, before, rows, bad, unsure)
        finally:
            p.write_text(original, encoding="utf-8")   # 原样写回，不动未提交改动


def report(rows, bad, unsure, only):
    for name, before, after in rows:
        flag = "OK  " if after != 0 else "空门"
        star = "  ⚠ 注入前已红，验证不充分" if (after != 0 and before != 0) else ""
        print(f"{flag} {name:18s} 注入前 exit={before} → 注入后 exit={after}{star}")
    for name, why in CANNOT_VERIFY.items():
        if not only or name in only:
            print(f"??   {name:18s} 本机/本档无法验证：{why}")


def main(argv) -> int:
    only = None
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            only = {s.strip() for s in argv[i + 1].split(",")}
    shutil.rmtree(PROBE, ignore_errors=True)      # 清掉上次崩溃的残留
    rows: list = []         # mypy 默认档：空列表要显式注解（类型门零容忍）
    bad: list = []
    unsure: list = []
    try:
        probe_files(only, rows, bad, unsure)
        probe_copies(only, rows, bad, unsure)
        probe_mutations(only, rows, bad, unsure)
    finally:
        git("rm", "-r", "--cached", "-q", str(PROBE))
        shutil.rmtree(PROBE, ignore_errors=True)
    report(rows, bad, unsure, only)
    if bad:
        print(f"GATE-PROBE FAIL 注入了也不红的门 = {bad}（空门/判据失效/注入不像）")
        return 1
    print(f"GATE-PROBE OK 可验的 {len(rows)} 道门都能被触发"
          + (f"；{len(unsure)} 道注入前已红（{unsure}）" if unsure else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
