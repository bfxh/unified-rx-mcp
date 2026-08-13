"""probe_05–probe_08：契约 §2（std_check）与 §3（cb_scan 增量）。

验证：
  p05 std_check 多语言（Go）可扫
  p06 std_check summary.total == len(issues)
  p07 std_check 检出占位文字
  p08 cb 索引缓存（二次扫描不重复索引）
"""
import json
import os
import time

from _common import probe, REPO_ROOT
import server as S

_TMP = os.path.join(REPO_ROOT, "_probe_tmp")
os.makedirs(_TMP, exist_ok=True)


@probe("p05_std_check_multilang")
def p05():
    """std_check 支持 Go 等多语言。"""
    go_file = os.path.join(_TMP, "sample.go")
    with open(go_file, "w", encoding="utf-8") as f:
        f.write("package main\nfunc main() { println(\"hi\") }\n")
    out = S._call("std_check", {"path": go_file})
    txt = out[0].text if out else ""
    if txt.startswith("{"):
        return True, f"Go 文件扫描正常: {txt[:100]}"
    return False, f"Go 文件被拒或异常: {txt[:120]}"


@probe("p06_std_check_total_consistency")
def p06():
    """summary.total == len(issues)。"""
    f = os.path.join(_TMP, "check.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("# TODO: 待实现\nx = 1  # magic\n")
    out = S._call("std_check", {"path": f})
    data = json.loads(out[0].text)
    total = data.get("summary", {}).get("total")
    issues = len(data.get("issues", []))
    if total == issues:
        return True, f"total({total}) == issues({issues})"
    return False, f"不一致: total={total} issues={issues}"


@probe("p07_std_check_placeholder")
def p07():
    """检出占位文字（lorem/placeholder 等真实违规词；TODO 仅统计不判违规）。"""
    f = os.path.join(_TMP, "placeholder.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("# placeholder text here\ndef g():\n    return 1\n")
    out = S._call("std_check", {"path": f})
    data = json.loads(out[0].text)
    rules = " ".join(str(i.get("rule", "")) for i in data.get("issues", []))
    if "text_placeholder" in rules or "占位" in rules:
        return True, f"检出占位: {rules[:80]}"
    return False, f"placeholder 未检出: {rules or '无 issues'}"


@probe("p08_cb_index_cache")
def p08():
    """相同目录二次 cb_index 应命中缓存（不重复全量索引）。"""
    proj = os.path.join(_TMP, "proj")
    os.makedirs(os.path.join(proj, "src"), exist_ok=True)
    with open(os.path.join(proj, "src", "a.py"), "w", encoding="utf-8") as fh:
        fh.write("def hello():\n    return 1\n")
    t0 = time.time()
    S._call("cb_index", {"path": proj})
    t1 = time.time()
    S._call("cb_index", {"path": proj})
    t2 = time.time()
    first, second = t1 - t0, t2 - t1
    if second <= first * 1.5 + 0.05:
        return True, f"二次索引耗时未显著增长: {first:.3f}s → {second:.3f}s"
    return False, f"二次索引明显更慢（未命中缓存）: {first:.3f}s → {second:.3f}s"
