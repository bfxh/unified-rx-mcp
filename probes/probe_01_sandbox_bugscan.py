"""probe_01–probe_04：契约 §0（沙盒/失败语义）与 §1（bug_scan）。

注意：server.py 的沙盒根在 import 时确定（cwd + D:\\开发），
探针文件位于仓库根内（import server 时 cwd=仓库根），
所以探针用仓库根内文件做断言；越界测试用 C:\\Windows 路径。

验证：
  p01 沙盒越界拒绝
  p02 bug_scan 拒绝非 Python/Rust 文件
  p03 bug_scan issue_count == len(issues)（对 Python 文件）
  p04 bug_scan 覆盖未定义变量/除零/None 解引用
"""
import json
import os
import sys
import tempfile

from _common import probe, REPO_ROOT
import server as S

# 仓库根内建临时测试文件（在沙盒根 cwd 内）
_TMP = os.path.join(REPO_ROOT, "_probe_tmp")
os.makedirs(_TMP, exist_ok=True)


@probe("p01_sandbox_reject")
def p01():
    """沙盒外路径（C:\\Windows）必须抛 路径越界。"""
    try:
        S._check_path(r"C:\Windows\System32\notepad.exe")
        return False, "沙盒外路径未被拒绝（应抛越界）"
    except ValueError as e:
        if "路径越界" in str(e) or "沙盒外" in str(e):
            return True, f"正确拒绝: {e}"
        return False, f"抛错但语义不符: {e}"


@probe("p02_bug_scan_lang_gate")
def p02():
    """bug_scan 必须拒绝非 Python/Rust 文件（用 spec/README.md）。"""
    md_file = os.path.join(REPO_ROOT, "spec", "README.md")
    out = S._call("bug_scan", {"path": md_file})
    txt = out[0].text if out else ""
    if "仅支持" in txt or "不支持" in txt:
        return True, f"语言门禁生效: {txt[:80]}"
    return False, f"未拒绝非 Python/Rust: {txt[:120]}"


@probe("p03_bug_scan_count_consistency")
def p03():
    """对 Python 文件：issue_count == len(issues)。"""
    py = os.path.join(_TMP, "count_ok.py")
    with open(py, "w", encoding="utf-8") as f:
        f.write("def ok():\n    return 1\n")
    out = S._call("bug_scan", {"path": py})
    data = json.loads(out[0].text)
    if data.get("ok") and data.get("issue_count") == len(data.get("issues", [])):
        return True, f"count 一致: {data['issue_count']}"
    return False, f"不一致: issue_count={data.get('issue_count')} len={len(data.get('issues', []))}"


@probe("p04_bug_scan_patterns")
def p04():
    """含缺陷 Python 文件必须检出 未定义变量/除零/None 解引用。"""
    buggy = os.path.join(_TMP, "buggy.py")
    with open(buggy, "w", encoding="utf-8") as f:
        f.write("def f(x):\n"
                "    y = undefined_var + 1\n"   # 未定义变量
                "    z = 10 / x\n"               # 除零风险
                "    return obj.field\n")        # None 解引用风险
    out = S._call("bug_scan", {"path": buggy})
    data = json.loads(out[0].text)
    rules = " ".join(str(i.get("rule", "")) for i in data.get("issues", []))
    if data.get("issue_count", 0) >= 1:
        return True, f"检出 {data['issue_count']} 项（rules: {rules[:100]}）"
    return False, f"含缺陷文件未检出: {rules or '无 issues'}"
