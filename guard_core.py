"""guard_core.py — 防幻觉引擎（纯标准库，零依赖，零网络）。

目标：AI 在引用事实（file:line / 符号 / 工具名 / 数字）前，先过本引擎验证。
输出三分级，杜绝"编造引用"与"凭空断言"：

  - verified      ：有本地证据（文件存在 / 行号在范围内 / 符号在文件内 / 工具在注册表）
  - refuted       ：有反证（文件不存在 / 行号越界 / 符号不在指定文件 / 工具不在注册表）——幻觉！
  - unverifiable  ：本地无法验证（诚实标注，不臆测、不冒充证据）

设计原则（对齐仓库既有约束）：
  - 纯静态、零 LLM、零网络——验证只依赖文件系统与调用方传入的注册表
  - 正确性优先：找不到 ≠ 不存在（跨文件符号无法判定时标 unverifiable 而非 refuted）
  - 输出 JSON 结构化，snippet 截断（≤160 字符），防上下文膨胀
"""

from __future__ import annotations

import json
import os
import re

# 文件大小上限（对齐 server._MAX_READ）
_MAX_READ = 1 << 20
_SNIPPET_LEN = 160

# ─────────────────────────────────────────────────────────────
# 声明提取（从 AI 文本中提取可验证事实）
# ─────────────────────────────────────────────────────────────

_PATH_LINE_RE = re.compile(r"(?<![A-Za-z0-9_/\\])([A-Za-z0-9_./\\-]+\.(?:py|rs|go|ts|js|md|json|toml|txt|java|c|h|cpp|hpp|gd|sh|yml|yaml)):(\d+)")
_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
# "符号在文件" / "文件 中 符号" / "文件 里 符号" 表述（符号+文件绑定验证，防跨文件误判）
# 表述含"定义在/定义于"时 defined=True（要求定义模式匹配，仅出现引用不算证据）
_SYMBOL_IN_FILE_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)`\s*(在|位于|出现在|定义在|定义于)\s*"
    r"([A-Za-z0-9_./\\-]+\.(?:py|rs|go|ts|js|md|json|toml|txt|java|c|h|cpp|hpp|gd|sh|yml|yaml))"
)
_SYMBOL_OF_FILE_RE = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:py|rs|go|ts|js|md|json|toml|txt|java|c|h|cpp|hpp|gd|sh|yml|yaml))\s*(?:中|里|文件)\s*"
    r"`([A-Za-z_][A-Za-z0-9_]*)`"
)
# 常见 TLD——URL（example.com/data.json:80 / sub.example.co.uk:80）误报为 file_line 时跳过
_URL_TLD_RE = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"  # 多级域名 + 顶级域（≥2 字符）
    r"(?:/|:)", re.IGNORECASE
)


def extract_claims(text: str) -> list[dict]:
    """从文本中提取声明，返回 [{kind, value, path?, line?, defined?}]。

    kind: file_line（path:line）/ symbol（反引号符号，尽量绑定文件上下文）。
    defined=True 表示表述为"定义在/定义于"（验证时要求定义模式，防 import 误判）。
    数字裸断言（如"49 个测试"）不自动提取——无法验证的数字不冒充证据，
    由调用方在 claims 参数里显式给出可对照项。
    """
    claims: list[dict] = []
    seen = set()

    for m in _PATH_LINE_RE.finditer(text or ""):
        p, ln = m.group(1), int(m.group(2))
        if _URL_TLD_RE.match(p):
            continue  # URL 端口段，非文件引用
        key = f"file_line|{p}|{ln}"
        if key in seen:
            continue
        seen.add(key)
        claims.append({"kind": "file_line", "value": f"{p}:{ln}", "path": p, "line": ln})

    for m in _SYMBOL_IN_FILE_RE.finditer(text or ""):
        s, verb, p = m.group(1), m.group(2), m.group(3)
        key = f"symbol|{s}|{p}"
        if key in seen:
            continue
        seen.add(key)
        seen.add(f"symbol|{s}")  # 已带路径提取的符号，裸提取时跳过（防重复条目）
        claims.append({"kind": "symbol", "value": s, "path": p,
                       "defined": verb in ("定义在", "定义于")})

    for m in _SYMBOL_OF_FILE_RE.finditer(text or ""):
        p, s = m.group(1), m.group(2)
        key = f"symbol|{s}|{p}"
        if key in seen:
            continue
        seen.add(key)
        seen.add(f"symbol|{s}")
        claims.append({"kind": "symbol", "value": s, "path": p, "defined": False})

    for m in _SYMBOL_RE.finditer(text or ""):
        s = m.group(1)
        key = f"symbol|{s}"
        if key in seen:
            continue
        seen.add(key)
        claims.append({"kind": "symbol", "value": s, "defined": False})

    return claims


def _norm_path(p: str, root: str | None) -> str | None:
    """把声明的路径规范化：相对 root 解析；Windows 风格反斜杠兼容。

    安全约束：解析结果必须落在 root 之下（防 .. 逃逸 / 任意绝对路径探测）。
    越界返回 None（由调用方按 unverifiable 处理，不泄露文件存在性）。
    """
    p2 = p.replace("\\", "/")
    if os.path.isabs(p2) or (len(p2) > 1 and p2[1] == ":"):
        if not root:
            return None  # 无 root 时拒绝绝对路径（无法锚定边界）
        cand = os.path.normpath(p2)
    elif root:
        cand = os.path.normpath(os.path.join(root, p2))
    else:
        return None
    root_abs = os.path.normpath(os.path.abspath(root))
    cand_abs = os.path.abspath(cand)
    if cand_abs == root_abs or cand_abs.startswith(root_abs + os.sep):
        return cand_abs
    return None


def _file_line_count(path: str) -> int | None:
    """精确统计行数：空文件=0；尾随换行不额外计数。失败返回 None。"""
    try:
        size = os.path.getsize(path)
        if size > _MAX_READ:
            return None
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return 0
        n = data.count(b"\n")
        if not data.endswith(b"\n"):
            n += 1
        return n
    except OSError:
        return None


def _grep_symbol(path: str, symbol: str, defined: bool = False) -> bool | None:
    """在文件内搜符号：defined=True 时要求定义模式（def/class/fn 等），
    仅出现在 import/调用行不算定义；行级包含命中返回 True。超 1MB → None。"""
    try:
        if os.path.getsize(path) > _MAX_READ:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if defined:
                    # 定义模式：可选修饰符（async/pub(crate)/public/static/export…）后跟 def|class|fn 等 + 符号名
                    m = re.match(
                        r"\s*(?:(?:pub\s*(?:\([^)]*\))?|export|static|async|public|private|protected|internal)\s+)*"
                        r"(?:def|class|fn|func|function|struct|enum|trait|impl|type|interface|const|let|var)\s+"
                        rf"{re.escape(symbol)}\b", line)
                    if m:
                        return True
                elif symbol in line:
                    return True
        return False
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────
# 验证
# ─────────────────────────────────────────────────────────────

def verify_claim(claim: dict, root: str | None = None,
                 tool_names: set | None = None) -> dict:
    """验证单条声明，返回 {claim, verdict, reason}。

    - file_line: 文件存在 + 行号在范围内 → verified；否则 refuted。
    - symbol:    工具名 → 查注册表；否则若伴文件（claim.path）→ 文件内搜；
                 文件内未找到 → refuted（限定文件内的符号声明可证伪）；
                 无文件上下文 → unverifiable（跨文件符号不臆断）。
    """
    kind = claim.get("kind")
    value = claim.get("value", "")
    result = {"claim": value, "kind": kind, "verdict": "unverifiable", "reason": ""}

    if kind == "file_line":
        path = _norm_path(claim.get("path", ""), root)
        line = claim.get("line")
        if not path:
            result["reason"] = "路径无法解析或越界（沙盒外，拒绝探测）"
            return result
        if not os.path.isfile(path):
            result.update(verdict="refuted", reason=f"文件不存在: {path}")
            return result
        total = _file_line_count(path)
        if total is None:
            result["reason"] = f"文件过大或不可读（>{_MAX_READ} 字节），不验证行号"
            return result
        if line is not None and (line < 1 or line > total):
            result.update(verdict="refuted",
                          reason=f"行号 {line} 越界（{path} 共 {total} 行，行号从 1 开始）")
            return result
        result.update(verdict="verified",
                      reason=f"文件存在（{total} 行），行号 {line} 在范围内")
        return result

    if kind == "symbol":
        if tool_names and value in tool_names:
            result.update(verdict="verified", reason=f"工具在注册表: {value}")
            return result
        path = _norm_path(claim.get("path", ""), root) if claim.get("path") else None
        if path and os.path.isfile(path):
            hit = _grep_symbol(path, value, defined=bool(claim.get("defined")))
            if hit is True:
                result.update(verdict="verified",
                              reason=f"符号{'(定义)' if claim.get('defined') else ''}出现在 {path}")
                return result
            if hit is False:
                result.update(verdict="refuted",
                              reason=f"符号 {value} 未出现在 {path}")
                return result
            result["reason"] = "文件过大，无法验证符号"
            return result
        if claim.get("path"):
            result["reason"] = "路径无法解析或越界（沙盒外，拒绝探测）"
            return result
        result["reason"] = "无文件上下文，符号是否存在无法本地判定（跨文件不臆断）"
        return result

    result["reason"] = f"未知声明类型: {kind}"
    return result


def verify_claims(claims: list[dict], root: str | None = None,
                  tool_names: set | None = None) -> dict:
    """批量验证，聚合输出 {total, verified, refuted, unverifiable, items}。

    verdict 判定（防幻觉核心裁决）：
      - pass：全部 verified 且零 unverifiable（全部声明有本地证据）
      - refuted：存在被证伪声明（幻觉，必须纠正）
      - unverified：存在无法验证声明（不得当事实传播，先取证）
      - no_claims：无声明可验证
    """
    items = [verify_claim(c, root=root, tool_names=tool_names) for c in claims]
    refuted = [i for i in items if i["verdict"] == "refuted"]
    unverified = [i for i in items if i["verdict"] == "unverifiable"]
    if refuted:
        verdict = "refuted"
    elif unverified:
        verdict = "unverified"
    elif items:
        verdict = "pass"
    else:
        verdict = "no_claims"
    return {
        "total": len(items),
        "verified": [i for i in items if i["verdict"] == "verified"],
        "refuted": refuted,
        "unverifiable": unverified,
        "items": items,
        "verdict": verdict,
    }


def guard_text(text: str, root: str | None = None,
               tool_names: set | None = None) -> dict:
    """一键入口：提取文本中的声明并验证。

    输出：{claims, verdict, verified, refuted, unverifiable, advice}
    advice 按结果给制止/放行指令（refuted → 必须纠正后才可继续）。
    """
    claims = extract_claims(text)
    if not claims:
        return {
            "claims": [], "verdict": "no_claims",
            "verified": [], "refuted": [], "unverifiable": [],
            "advice": "文本中未发现可验证声明（file:line/反引号符号）。"
                      "无证据的断言不视为事实；引用代码位置请给出 file:line 以便验证。",
        }
    res = verify_claims(claims, root=root, tool_names=tool_names)
    advice = (
        "全部声明有本地证据 ✓ 可放心引用"
        if res["verdict"] == "pass"
        else (
            "发现被证伪的声明（幻觉）✗ 必须纠正 refuted 条目后才能继续，"
            "不得引用错误位置/符号。"
            if res["verdict"] == "refuted"
            else (
                "部分声明本地无法验证——不得当作事实传播，先取证再引用。"
                if res["verdict"] == "unverified"
                else "无声明可验证。"
            )
        )
    )
    res["advice"] = advice
    res["claims"] = [c["value"] for c in claims]
    return res


def capability_manifest(core_tools: list[dict], ext_tools: list[dict]) -> dict:
    """能力清单（动态生成，不写死）：AI 分清楚"自己有什么、没有什么"。

    core_tools/ext_tools: [{"name", "desc"}]，由 server 从注册表实时构建。
    has_not 是显式边界声明——防止 AI 幻觉自己具备不存在的能力。
    """
    return {
        "generated_from": "live registry",
        "has": {
            "core_tools": core_tools,
            "ext_tools": ext_tools,
            "capabilities": [
                "本地文件安全读写（路径校验 + 大小上限）",
                "代码库索引/变更感知/符号定位",
                "静态 bug 扫描 + traceback 定位（纯 AST，零执行）",
                "工程标准检查（占位文字/命名冲突/UI 硬编码/魔法数字）",
                "Bevy UI 静态检查",
                "LSE 教训召回/反馈（防复发闭环）",
                "PR→测试影响映射 / 变异测试（扩展）",
                "LSP 语义查询（补全/悬停/定义/引用，扩展）",
                "工具链协作（pipeline/parallel）+ 结构化回喂（tool_card）",
                "纯函数计算（数学/文本/JSON/排序/统计/素数）",
            ],
        },
        "has_not": [
            "不能联网（无网络请求能力，纯本地）",
            "不能执行任意代码（无 eval/exec/subprocess 调用）",
            "不能访问沙盒外路径（受 UNIFIED_RX_SANDBOX 约束，未设置=不限制）",
            "不能读取文件内容之外的外部数据（无数据库/API/浏览器）",
            "不能替代 LLM 推理（工具只产出证据与事实，不做模型判断）",
            "不能修改代码库索引之外的文件（fs_write 受大小上限约束）",
        ],
        "boundaries": {
            "file_read_write_max_bytes": _MAX_READ,
            "array_max_items": 100_000,
            "factorial_max_n": 1000,
            "prime_max": 1_000_000,
            "parallel_max_concurrency": 8,
            "regex_blacklist": "嵌套量词/开区间链（ReDoS 防护）",
        },
        "usage": "对话开始时调用一次本清单，明确能力边界；引用 file:line 前用 hallucination_guard 验证。",
    }


def main() -> None:
    """CLI 自检（与 server.py --selftest 风格一致）。"""
    text = "bug 在 server.py:1604 与 `_tool_bug_scan` 附近；工具 `fs_read` 可用；文件 no_such.py:99"
    root = os.path.dirname(os.path.abspath(__file__))
    tools = {"fs_read", "_tool_bug_scan", "bug_scan"}
    res = guard_text(text, root=root, tool_names=tools)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("OK: guard_core selftest")


if __name__ == "__main__":
    main()
