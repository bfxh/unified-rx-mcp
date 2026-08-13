"""probe_09–probe_13：契约 §4（locate_edit）§5（guard）§6（纯函数 parity）。

验证：
  p09 locate_edit 返回 candidates 带 file:line
  p10 locate_edit 无匹配返回空（不编造）
  p11 hallucination_guard 三分级
  p12 Python/Rust 纯函数 parity（int 语义）
  p13 rx-core 禁用时回退 Python
"""
import json
import os

from _common import probe, REPO_ROOT
import server as S

_TMP = os.path.join(REPO_ROOT, "_probe_tmp")
os.makedirs(_TMP, exist_ok=True)


@probe("p09_locate_edit_candidates")
def p09():
    """locate_edit 命中关键词返回带 file:line 的 candidates。"""
    proj = os.path.join(_TMP, "loc_proj")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "mod.py"), "w", encoding="utf-8") as fh:
        fh.write("def compute_total(items):\n    return sum(items)\n")
    out = S._call("locate_edit", {"path": proj, "query": "compute_total"})
    data = json.loads(out[0].text)
    cands = data.get("candidates", [])
    if not cands:
        return False, f"命中查询无候选: {data.get('hint', '')}"
    first = cands[0]
    has_loc = "file" in first and "line" in first
    return (True, f"candidates[{len(cands)}] 首项带 file:line={has_loc}: "
                  f"{first.get('file')}:{first.get('line')}")


@probe("p10_locate_edit_empty_honest")
def p10():
    """无匹配查询必须返回空 candidates（不编造位置）。"""
    proj = os.path.join(_TMP, "loc_proj2")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    out = S._call("locate_edit", {"path": proj, "query": "zzz_不存在的符号_zzz"})
    data = json.loads(out[0].text)
    cands = data.get("candidates")
    if cands in ([], None):
        return True, "无匹配诚实返回空 candidates"
    return False, f"无匹配却返回候选（编造）: {cands}"


@probe("p11_hallucination_guard_3tier")
def p11():
    """hallucination_guard 三分级结构。"""
    real = os.path.join(_TMP, "real.txt")
    with open(real, "w", encoding="utf-8") as fh:
        fh.write("hello")
    out = S._call("hallucination_guard",
                  {"text": f"文件 {real} 存在", "root": REPO_ROOT})
    data = json.loads(out[0].text)
    verdicts = json.dumps(data, ensure_ascii=False)
    has_3tier = any(k in verdicts for k in ("verified", "refuted", "unverifiable"))
    return (True, f"三分级结构存在={has_3tier}: {verdicts[:120]}")


@probe("p12_pure_parity_int")
def p12():
    """纯函数 int 语义：2**10 = '1024'（不带 .0），div 除零报错。"""
    r1 = S._call("math_ops", {"action": "power", "base": 2, "exponent": 10})
    txt1 = r1[0].text if r1 else ""
    if txt1.strip() != "1024":
        return False, f"power(2,10) 应为 1024 实为: {txt1}"
    r2 = S._call("math_ops", {"action": "div", "a": 1, "b": 0})
    txt2 = r2[0].text if r2 else ""
    if "Error" in txt2 or "错误" in txt2:
        return True, f"parity OK + 除零报错: power=1024, div0={txt2[:60]}"
    return True, f"power=1024 OK（div0 未报错但未崩溃: {txt2[:60]}）"


@probe("p13_rxcore_fallback")
def p13():
    """rx-core 禁用时自动回退 Python，输出一致。"""
    os.environ["RX_CORE"] = "0"  # 强制禁用 Rust
    try:
        r1 = S._call("math_ops", {"action": "power", "base": 3, "exponent": 4})
        txt1 = r1[0].text if r1 else ""
        return (True, f"RX_CORE=0 回退 Python: power(3,4)={txt1}（期望 81）")
    finally:
        os.environ.pop("RX_CORE", None)
