# -*- coding: utf-8 -*-
"""历史明文扫描（S147）：近 N 个提交的 **diff 面**不得出现 critical/high 明文。

为什么补这一道：现有 secrets gate 只扫**工作树**、GitHub push protection 只管
**新推送**——历史的"加进去又删掉"的明文（改史前的残留）没有门。本脚本把
`git log -p` 的 diff 按文件块切分、剔除既有豁免面（tests/ 夹具；纪律与树扫一致），
落到临时文本后用 secrets_hunt 扫（自给自足沙盒，同 ci_secrets_gate.py 惯例）。

用法：python -X utf8 scripts/secrets_history.py [--n 50]
env：UNIFIED_RX_HISTORY_N 覆盖默认窗口。
"""
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
        r = registry.call("secrets_hunt", {"path": str(tmp), "max_files": 10,
                                           "max_results": 500})
        if not r.get("ok"):
            sys.exit(f"secrets_hunt 调用失败: {r.get('error')}")
        hits = r["result"].get("hits") or []
        bad = [h for h in hits if h.get("severity") in ("critical", "high")]
        print(f"HISTORY-SCAN n={n} commits={commits} blocks={blocks} "
              f"hits={len(hits)} 红线={len(bad)}")
        for h in bad[:10]:
            print(f"  {h.get('severity')} {h.get('kind')} @ {h.get('file')}:{h.get('line')}")
        if bad:
            sys.exit("SECRETS-HISTORY FAIL: 历史 diff 有红线明文（豁免面之外）")
        print("SECRETS-HISTORY OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
