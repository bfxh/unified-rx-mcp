# -*- coding: utf-8 -*-
"""s108_resolve_compare.py —— 文本级 vs 解析级引用对比（NAMERES §六 验收③）。

口径：
- 解析级引用 = `rx-scan resolve <file>` 的 edges（kind ∈ local/module）→ (file,line)
- 文本级引用 = `locate_edit`（忽略大小写、含注释/字符串）→ (file,line)
- 差异分类：
  - text_only：文本命中但解析未命中 → 预期为注释/字符串/大小写变体/属性名
  - resolved_only：解析命中但文本未命中 → 异常信号（文本应至少覆盖解析）
输出：stdout 一行 JSON（供重定向留档）；样例含命中行原文供人工抽查。
用法：python bench/s108_resolve_compare.py [root] [top_n]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("UNIFIED_RX_SANDBOX", "*")   # 本机分析脚本显式声明
import registry  # noqa: E402
import tools     # noqa: F401,E402
from tools import scan as scan_mod  # noqa: E402


def _inside(path, root):
    """realpath 包含性校验：path 必须落在 root 内（否则拒调）。"""
    rp = os.path.realpath(root)
    ap = os.path.realpath(path)
    return ap == rp or ap.startswith(rp + os.sep), ap


def resolve_file(path, root):
    """argv 列表直调（shell=False）、路径先过包含性校验。"""
    exe = scan_mod._rx_scan_exe()
    if not exe:
        return None
    ok, ap = _inside(path, root)
    if not ok:
        return None
    cp = subprocess.run([exe, "resolve", ap], capture_output=True, timeout=120,
                        shell=False)
    lines = (cp.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return json.loads(lines[-1]) if lines else None


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tools")
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    # 1) 收集解析级引用
    refs = {}          # name -> set[(rel, line)]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "__pycache__", "node_modules", "target")]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    for fp in files:
        out = resolve_file(fp, root)
        if not out or out.get("error"):
            continue
        for e in out.get("edges", []):
            if e["kind"] in ("local", "module"):
                rel = os.path.relpath(fp, root).replace("\\", "/")
                refs.setdefault(e["name"], set()).add((rel, e["line"]))

    # 2) 取引用最多的 top_n 个符号，逐个做文本级对比
    top = sorted(refs.items(), key=lambda kv: -len(kv[1]))[:top_n]
    rows, tot_text, tot_res, tot_text_only, tot_res_only = [], 0, 0, 0, 0
    samples = []
    for name, rset in top:
        # 文本级命中分页取全（locate_edit 自身 limit + registry 200 条钳制）
        hits = set()
        cursor = 0
        capped = False
        while True:
            r = registry.call("locate_edit", {"path": root, "query": name,
                                              "max_files": 500, "limit": 5000,
                                              "cursor": cursor})
            res = r.get("result") or {}
            for h in res.get("hits", []):
                rel = os.path.relpath(h["file"], root).replace("\\", "/")
                hits.add((rel, h["line"]))
            if res.get("truncated") and res.get("next_cursor"):
                cursor = res["next_cursor"]
            else:
                break
        capped = len(hits) >= 5000      # 命中数触顶 → 该行 resolved_only 不可判
        text_only = hits - rset
        res_only = rset - hits
        tot_text += len(hits)
        tot_res += len(rset)
        tot_text_only += len(text_only)
        tot_res_only += len(res_only)
        rows.append({"name": name, "resolved": len(rset), "text": len(hits),
                     "text_only": len(text_only),
                     "resolved_only": None if capped else len(res_only),
                     "capped": capped})
        for rel, line in sorted(text_only)[:3]:
            try:
                ok, full = _inside(os.path.join(root, rel), root)
                content = open(full, encoding="utf-8", errors="replace").read() \
                    .splitlines()[line - 1] if ok else ""
            except (OSError, IndexError):
                content = ""
            samples.append({"name": name, "file": rel, "line": line,
                            "text": content.strip()[:100]})
        for rel, line in sorted(res_only)[:3]:
            samples.append({"name": name, "file": rel, "line": line,
                            "resolved_only": True})

    print(json.dumps({
        "root": root, "files": len(files), "symbols": len(rows),
        "totals": {"resolved_refs": tot_res, "text_hits": tot_text,
                   "text_only": tot_text_only, "resolved_only": tot_res_only},
        "rows": rows, "samples": samples[:60],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
