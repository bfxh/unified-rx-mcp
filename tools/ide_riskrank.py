# -*- coding: utf-8 -*-
"""tools/ide_riskrank.py —— ide_risk_rank 风险榜（S129，CONSOLIDATION §四 P1-A）。

把 SCAN-POLICY「上帝对象拆分大于测试」变成**自动排序**：高扇入 × 无测试的
文件排在前面——拆分/补测的优先级不再拍脑袋。

数据来源（全部既有、单一实现）：
- **扇入**：Rust 调用图（nameres 同一作用域引擎，tools/ide_callgraph 的薄壳）；
- **测试存在性**：静态文件约定代理（与 code_review 覆盖透镜共用
  `test_candidates` 口径；rust 认内联 `#[cfg(test)]`）；
- 记账：每轮结果存 JSONL（默认 ~/.unified-rx/risk_history.jsonl），
  `mode=history` 出趋势（总扇入/无测试数 的首末对比）——还 HARDENING
  「覆盖率趋势」欠账的静态版。

诚实定界（写进输出 note）：
- "无测试" = 静态约定代理，**非实测行覆盖**（实测走 code_coverage 单点）；
- 扇入 = 调用图**可解析**调用边计数（不可解析调用如实缺席，见 CALLGRAPH.md）；
- 分数 = 扇入 ×(无测试 ? 2 : 1)——权重是启发式，公式随输出透明给出。
"""
import json
import os
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve
from tools.scan import _rx_scan_exe
from tools.ide_callgraph import _rx_callgraph
from tools.filewalk import SCAN_WALK_SKIP, iter_files
from tools.code_review import test_candidates

# 测试文件判定（默认不计入榜单——它们不是拆分对象）
_RISK_EXTRA_SKIP = ("venv", ".venv", ".pytest_cache")


def _is_test_path(rel):
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")]
    base = parts[-1]
    stem = os.path.splitext(base)[0]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return (stem.startswith("test_") or stem.endswith("_test")
            or base == "conftest.py")


def _repo_files(root, cap=20000):
    """仓内文件基名集合（小写）——测试存在性判定用（同 code_review 口径）。"""
    out = set()
    for fp in iter_files(root, cap, lambda _fp: True,
                         tuple(SCAN_WALK_SKIP) + _RISK_EXTRA_SKIP):
        out.add(os.path.basename(fp).lower())
    return out


def _has_test(root, rel, repo_files):
    """测试存在性（静态代理）：py/go/java 认文件约定，rust 认内联 cfg(test)。"""
    full = os.path.join(root, rel.replace("/", os.sep))
    ext = os.path.splitext(rel)[1].lower()
    stem = os.path.splitext(os.path.basename(rel))[0]
    if ext == ".rs":
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return "#[cfg(test)]" in f.read()
        except OSError:
            return None
    if any(c in repo_files for c in test_candidates(stem, ext)):
        return True
    # 跨扩展名候选（test_<stem>.<任意扩展>）兜一层
    for c in repo_files:
        if c.startswith(f"test_{stem}.") or c == f"{stem}_test{os.path.splitext(c)[1]}":
            return True
    return False


def _callgraph_or_error(root, max_files):
    if _rx_scan_exe() is None:
        return None, ("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                      "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    raw = _rx_callgraph(root, max_files)
    if not isinstance(raw, dict) or raw.get("error"):
        err = raw.get("error") if isinstance(raw, dict) else raw
        return None, f"调用图失败: {err}"
    return raw, None


def _rank_rows(root, max_files, top, include_tests):
    raw, err = _callgraph_or_error(root, max_files)
    if raw is None:
        return None, err
    # 每被调定义（to_file,to_line,短名）的去重调用点集合 → 文件级聚合
    defs = {}
    for e in raw.get("edges") or []:
        if not isinstance(e, dict):
            continue
        to_file = e.get("to_file")
        if not to_file:
            continue
        name = (e.get("callee") or "").rsplit(".", 1)[-1]
        key = (to_file, int(e.get("to_line") or 0), name)
        defs.setdefault(key, set()).add((e.get("file"), e.get("line")))
    by_file = {}
    for (to_file, _to_line, name), sites in defs.items():
        if not sites:
            continue
        row = by_file.setdefault(to_file, {"file": to_file, "fan_in": 0,
                                           "defs": {}, "sites": 0})
        row["fan_in"] += len(sites)
        row["sites"] += len(sites)
        prev = row["defs"].get(name, 0)
        row["defs"][name] = prev + len(sites)
    repo_files = _repo_files(root)
    rows = []
    for rel, row in by_file.items():
        if not include_tests and _is_test_path(rel):
            continue
        has_test = _has_test(root, rel, repo_files)
        score = row["fan_in"] * (1 if has_test else 2)
        top_def = max(row["defs"].items(), key=lambda kv: (kv[1], kv[0]))
        rows.append({"file": rel, "fan_in": row["fan_in"],
                     "defs": len(row["defs"]), "call_sites": row["sites"],
                     "top_def": {"name": top_def[0], "fan_in": top_def[1]},
                     "has_test": has_test, "score": score})
    rows.sort(key=lambda r: (-r["score"], -r["fan_in"], r["file"]))
    return rows[:int(top)], None


@tool("ide_risk_rank",
      "风险榜（S129）：高扇入 × 无测试文件 → 拆分/补测优先级自动排序"
      "（SCAN-POLICY『拆分大于测试』的机器化）。扇入=调用图可解析调用边；"
      "无测试=静态约定代理（非实测行覆盖）；record 存 JSONL，mode=history 出趋势",
      "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "项目根目录（沙盒内）"},
           "top": {"type": "integer", "description": "榜单条数（默认 20，上限 100）"},
           "mode": {"type": "string", "enum": ["rank", "history"],
                    "description": "rank=出榜（默认）；history=读历史趋势"},
           "record": {"type": "boolean", "description": "rank 模式是否记入 JSONL（默认 true）"},
           "include_tests": {"type": "boolean",
                             "description": "榜单是否含测试文件（默认 false）"},
           "history_dir": {"type": "string",
                           "description": "历史目录（默认 ~/.unified-rx）"},
           "max_files": {"type": "integer", "description": "调用图文件上限（默认 500）"},
       },
       "required": ["root"]})
def ide_risk_rank(root, top=20, mode="rank", record=True, include_tests=False,
                  history_dir=None, max_files=500):
    try:
        root = _fs_resolve(root)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    hdir = history_dir or os.path.join(os.path.expanduser("~"), ".unified-rx")
    hfile = os.path.join(hdir, "risk_history.jsonl")
    top = max(1, min(int(top), 100))

    if mode == "history":
        runs = []
        try:
            with open(hfile, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if os.path.normcase(rec.get("root", "")) == os.path.normcase(root):
                        runs.append(rec)
        except OSError:
            pass
        runs = runs[-20:]
        delta = None
        if len(runs) >= 2:
            a, b = runs[0], runs[-1]
            delta = {"total_fanin": b.get("total_fanin", 0) - a.get("total_fanin", 0),
                     "untested_files": (b.get("untested_files", 0)
                                        - a.get("untested_files", 0)),
                     "window": f"{a.get('ts')} → {b.get('ts')}"}
        return {"root": root, "mode": "history", "runs": runs,
                "delta": delta, "history_file": hfile,
                "note": "趋势=同一 root 的历史记录首末对比；空历史=还没跑过 rank"}

    rows, err = _rank_rows(root, int(max_files), top, include_tests)
    if rows is None:
        return {"error": err}
    total_fanin = sum(r["fan_in"] for r in rows)
    untested = sum(1 for r in rows if r["has_test"] is False)
    recorded = False
    if record:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "root": root,
               "rows": len(rows), "total_fanin": total_fanin,
               "untested_files": untested,
               "top": [[r["file"], r["score"]] for r in rows[:5]]}
        try:
            os.makedirs(hdir, exist_ok=True)
            with open(hfile, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            recorded = True
        except OSError:
            recorded = False             # 记账失败不拖垮榜单（如实标注）
    return {"root": root, "mode": "rank", "rows": rows,
            "totals": {"files": len(rows), "total_fanin": total_fanin,
                       "untested_files": untested},
            "recorded": recorded, "history_file": hfile,
            "formula": "score = 扇入 ×(无测试 ? 2 : 1)",
            "note": "无测试=静态文件约定代理（非实测行覆盖，实测用 code_coverage）；"
                    "扇入=调用图可解析调用边（不可解析如实缺席）；"
                    "优先看榜首：高扇入+无测试=拆分或补测的第一优先级"}
