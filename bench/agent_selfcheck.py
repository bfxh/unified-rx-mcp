# -*- coding: utf-8 -*-
"""agent_selfcheck —— S8 复用入口：对任意已安装智能体执行 克隆→隔离审计。

用户规则固化：以后遇到智能体类应用，默认动作就是本脚本——先复制到隔离沙箱，
审计只碰副本，原件零接触。

用法：
  python bench/agent_selfcheck.py "D:\\rj\\AI\\Yan Agent" \\
         --data-dir "C:\\Users\\lbx13\\AppData\\Roaming\\yan-agent" [--clean] [--json]

返回码：0 干净（无 definite）｜1 工具/调用失败｜2 存在 definite 发现。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry   # noqa: E402
import tools      # noqa: F401,E402  注册全部域


def _run_one(tag, source, clean):
    cr = registry.call("app_clone", {"source_dir": source})
    if not cr.get("ok"):
        print(f"[{tag}] CLONE FAIL: {cr.get('error')}")
        return None
    snap = cr["result"]
    print(f"[{tag}] 克隆完成: files={snap['files']} bytes={snap['bytes']:,} "
          f"verified={snap['verified']} -> {snap['snapshot']}")
    ar = registry.call("app_audit", {"snapshot_dir": snap["snapshot"]})
    if not ar.get("ok"):
        print(f"[{tag}] AUDIT FAIL: {ar.get('error')}")
        registry.call("app_clean", {"target": snap["snapshot"], "__authorized": True})
        return None
    res = ar["result"]

    def brief(f):
        d = f["detail"]
        return d if len(d) <= 60 else d[:57] + "..."

    print(f"[{tag}] definite={res['definite']} clues={res['clues']} "
          f"hit_lines={res['hit_lines']} binaries={res['binaries_total']}")
    for f in [f for f in res["findings"] if f["kind"] == "definite"][:15]:
        print(f"    DEFINITE {f['label']:<22} {f['file']}:{f['line']} :: {brief(f)}")
    shown = 0
    for f in res["findings"]:
        if f["kind"] != "clue":
            continue
        print(f"    clue      {f['label']:<22} {f['file']}:{f['line']} :: {brief(f)}")
        shown += 1
        if shown >= 25:
            print(f"    ... 其余 clue 略（共 {res['clues']} 条）")
            break
    for a in res.get("asar", []):
        ext = a.get("extracted", "?")
        err = "" if "error" not in a else " error=" + str(a["error"])
        line = f"    asar {a['asar']}: {ext} 条目已提取重扫{err}"
        print(line[:160])
    print(f"    URL Top: " + ", ".join(
        f"{u['host']}({u['count']})" for u in res["url_host_top"][:8]))
    if clean:
        clr = registry.call("app_clean", {"target": snap["snapshot"], "__authorized": True})
        print(f"[{tag}] 已清理副本: removed={clr.get('result', {}).get('removed')}"
              if clr.get("ok") else f"[{tag}] 清理失败: {clr.get('error')}")

    try:
        registry.call("scan_log", {"action": "record", "record": {
            "root": source,
            "summary": f"S8 agent_selfcheck {tag}: clone files={snap['files']}, "
                       f"definite={res['definite']}, clues={res['clues']}",
        }})
    except Exception as e:  # 日志失败不阻断
        print(f"[{tag}] scan_log 记录失败（忽略）: {e.__class__.__name__}")
    return res


def main():
    try:  # GBK 控制台下中文输出不炸码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="智能体克隆隔离自查（S8）")
    ap.add_argument("source_dir", help="应用安装目录")
    ap.add_argument("--data-dir", help="可选：用户数据目录（第二克隆）")
    ap.add_argument("--clean", action="store_true", help="审完即清理副本")
    ap.add_argument("--json", action="store_true", help="末尾输出完整 JSON 报告")
    args = ap.parse_args()

    exit_code = 0
    reports = []
    for i, (tag, src) in enumerate([("prog", args.source_dir)] +
                                   ([("data", args.data_dir)] if args.data_dir else [])):
        res = _run_one(tag, src, args.clean)
        if res is None:
            exit_code = max(exit_code, 1)
            continue
        reports.append({"tag": tag, **res})
        if res["definite"]:
            exit_code = 2
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, default=str)[:50000])
    print(f"EXIT={exit_code} （0 干净 / 1 调用失败 / 2 有 definite 待人工确认）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
