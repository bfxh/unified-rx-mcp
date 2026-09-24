"""历史明文扫描（S147）：近 N 个提交的 **diff 面**不得出现 critical/high 明文。

为什么补这一道：现有 secrets gate 只扫**工作树**、GitHub push protection 只管
**新推送**——历史的"加进去又删掉"的明文（改史前的残留）没有门。本脚本把
`git log -p` 的 diff 按文件块切分、剔除既有豁免面（tests/ 夹具；纪律与树扫一致），
落到临时文本后用 secrets_hunt 扫（自给自足沙盒，同 ci_secrets_gate.py 惯例）。

用法：python -X utf8 scripts/secrets_history.py [--n 50]
env：UNIFIED_RX_HISTORY_N 覆盖默认窗口。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIFIED_RX_SANDBOX", str(ROOT))   # 自给自足（同 secrets 门纪律）

import registry  # noqa: E402
import tools  # noqa: E402,F401

_SKIP_PREFIX = ("tests/",)          # 与树扫豁免一致：测试夹具假值运行时拼接


def history_diff(n):
    cp = subprocess.run(["git", "-C", str(ROOT), "log", "-p", f"-{n}", "--no-color",
                         "--pretty=format:@@COMMIT %h %s"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300)
    if cp.returncode != 0:
        sys.exit(f"git log 失败: {cp.stderr[-300:]}")
    return cp.stdout or ""


def keep_blocks(text):
    """按 `diff --git` 切块，保留非豁免面（返回拼接文本 + 块/提交计数）。"""
    out, blocks, commits = [], 0, 0
    cur, cur_path = [], None

    def _flush():
        nonlocal blocks
        if cur and cur_path and not cur_path.startswith(_SKIP_PREFIX):
            out.extend(cur)
            blocks += 1

    for line in text.splitlines():
        if line.startswith("@@COMMIT"):
            commits += 1
            _flush()
            cur, cur_path = [], None
            continue
        if line.startswith("diff --git "):
            _flush()
            cur, cur_path = [line], None
            continue
        if line.startswith("+++ b/"):
            cur_path = line[6:].strip()
        cur.append(line)
    _flush()
    return "\n".join(out), blocks, commits


def _dump_path(base):
    """临时 dump 路径：resolve 后必须在给定根内（写前显式校验，防穿越）。"""
    q = (base / "history_dump.py").resolve()
    if base not in q.parents:
        raise ValueError(f"dump 路径越界: {q}")
    return q


EXEMPT = Path(__file__).resolve().parent.parent / "spec" / "secrets-exempt.json"


def load_exempt():
    """白名单：**指定文件 + 指定字面量**才豁免，且每条必须写 why（空 why = 红）。

    历史 diff 里已经存在的东西没法靠「以后改掉」清除——比如审计报告正文里为了说明
    「扫描器认什么」而**引用**的第三方假值字面量，它在引入它的那个提交的 diff 里永远命中。
    故留一个窄口子：只豁免列明的那几串字面量，同一文件里出现别的明文照旧红。

    字面量在文件里存**碎片**（`contains_parts`），运行时拼回整串：这份清单本身也在仓库里，
    写成整串就会被工作树侧的 secrets 门判成「测试区外的明文」（实测被拦过）——
    与本仓 S123 纪律（敏感形状运行时拼接）一致。
    """
    if not EXEMPT.is_file():
        return []
    doc = json.loads(EXEMPT.read_text(encoding="utf-8"))
    out = []
    for e in doc.get("entries") or []:
        parts = e.get("contains_parts")
        if not parts:
            sys.exit(f"SECRETS-HISTORY FAIL: 白名单条目缺 contains_parts（{e.get('path')}）"
                     f"——字面量必须以碎片形式给出（整串会被工作树侧的门拦下）")
        out.append({"path": e.get("path"), "contains": "".join(parts), "why": e.get("why")})
    return out


def _file_of_line(lines, lineno):
    """命中行（dump 内行号）落在哪个源文件：取它前面最近的 `+++ b/<path>`。"""
    for i in range(min(lineno, len(lines)) - 1, -1, -1):
        t = lines[i]
        if t.startswith("+++ b/"):
            return t[6:].strip()
        if t.startswith("diff --git "):
            return None
    return None


def classify(hits, lines, exempt):
    """把命中分成 (红线, 已豁免)。豁免条件：属于白名单里的文件 **且** 命中行含白名单字面量。"""
    bad, waived = [], []
    for h in hits:
        if h.get("severity") not in ("critical", "high"):
            continue
        lineno = h.get("line") or 0
        text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        src = _file_of_line(lines, lineno)
        for e in exempt:
            if src == e.get("path") and e.get("contains") and e["contains"] in text:
                waived.append(f"{src}:{lineno}（{e.get('why', '')[:60]}）")
                break
        else:
            bad.append(h)
    return bad, waived


def main(argv):
    n = int(os.environ.get("UNIFIED_RX_HISTORY_N", "50"))
    for i, a in enumerate(argv):
        if a == "--n" and i + 1 < len(argv):
            n = int(argv[i + 1])
    diff, blocks, commits = keep_blocks(history_diff(n))
    # dump 落**仓内**临时目录：沙盒(ROOT)语义不放宽（首版落 %TEMP% 被沙盒正确拒绝），
    # 路径仍经 _dump_path 写前校验；finally 必删，不留痕。
    tmp = Path(tempfile.mkdtemp(prefix=".urx-hist-", dir=str(ROOT))).resolve()
    try:
        dump = _dump_path(tmp)
        dump.write_text(diff, encoding="utf-8")      # 后缀进 include 面
        # 金丝雀：往 dump 末尾钉一行「必被扫到」的明文。扫描器有单文件体积上限（默认 512KB）
        # ——2026-09-21 实测：n=50 的 dump 是 1144KB，旧版被**静默跳过**、hits=0、还照印
        # 「SECRETS-HISTORY OK」⇒ 默认档的门一直在空转（n=30 时才 491KB、真被扫到才暴露）。
        # 故两手都要：① 按 dump 实际体积给上限；② 金丝雀没被扫到 = 红（把空转变成可判）。
        canary = "-----BEGIN " + "PRIVATE KEY-----"   # 运行时拼接（S123 纪律）：
        # 本脚本自己的 diff 也不该自带可命中的字面量——否则这个提交一进窗口就自伤。
        with dump.open("a", encoding="utf-8") as fh:
            fh.write(f"# canned self-test marker {canary}\n")
        text = dump.read_text(encoding="utf-8")
        dump_kb = max(1, len(text.encode("utf-8")) // 1024)
        canary_no = len(text.splitlines())
        r = registry.call("secrets_hunt", {"path": str(tmp), "max_files": 10,
                                           "max_file_kb": max(512, dump_kb + 64),
                                           "max_results": 500})
        if not r.get("ok"):
            sys.exit(f"secrets_hunt 调用失败: {r.get('error')}")
        hits = r["result"].get("hits") or []
        if not any(h.get("line") == canary_no and h.get("severity") in ("critical", "high")
                   for h in hits):
            sys.exit("SECRETS-HISTORY FAIL: 金丝雀没被扫到——这份历史 dump 没有被真正扫描"
                     "（体积超上限被跳过之类），门等于空转")
        lines = text.splitlines()
        exempt = load_exempt()
        for e in exempt:
            if not e.get("why"):
                sys.exit(f"SECRETS-HISTORY FAIL: 白名单条目缺 why（{e.get('path')}）——"
                         f"豁免必须写明理由，理由空着等于没登记")
        bad, waived = classify(hits, lines, exempt)
        bad = [h for h in bad if h.get("line") != canary_no]   # 金丝雀自身不计入红线
        print(f"HISTORY-SCAN n={n} commits={commits} blocks={blocks} dump={dump_kb}KB "
              f"hits={len(hits)} 红线={len(bad)} 豁免={len(waived)}")
        for h in bad[:10]:
            print(f"  {h.get('severity')} {h.get('kind')} @ {h.get('file')}:{h.get('line')}"
                  f" ← {_file_of_line(lines, h.get('line') or 0)}")
        for w in waived[:5]:
            print(f"  ※ 已豁免：{w}")
        if bad:
            sys.exit("SECRETS-HISTORY FAIL: 历史 diff 有红线明文（豁免面之外）")
        print("SECRETS-HISTORY OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
