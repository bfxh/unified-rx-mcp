# -*- coding: utf-8 -*-
"""tools/scan.py —— 扫描域壳工具（5 工具）：bug_scan / std_check / ui_check / bug_locate / project_scan

收敛自旧版 vuln_scan/scan_all/scan_now/scan_delta → project_scan 组合。
P3 增强（2026-08-24）：Rust 生产规则、ui_check 三引擎。
P4 增强（2026-08-24）：Bevy 专项规则（用户：引擎重点优化 Bevy）。
P5 修复（2026-08-25）：Python AST 作用域感知（参数/方法/属性/魔法方法不算未定义）——
  消除 undefined_name 假阳性（592→0 级）；bug_locate 提取 traceback 文件:行号。
S82（2026-09-05）：std_check / ui_check / bug_locate Rust 原生化（rx-scan.exe，
  见 rust/src/scan.rs）——Python 侧只留薄壳转调，exe 缺失报清晰错误不静默降级。
S83（2026-09-05）：bug_scan 全量原生化（rust/src/bug.rs + 手写迷你解析器 pyast.rs，
  rx-scan bugscan 子命令）——scan.py 至此四工具皆薄壳；ast_scan 仍 Python（后续轮）。
S127（2026-09-14）：CONSOLIDATION §三 P0 上帝对象拆分——**评审域整体平移**
  tools/code_review.py（code_review + 11 个透镜助手；此前双域一文件 650 行、
  30 天 30 提交全仓最高）。本文件还原为纯壳域；两侧同批清理两个零引用死常量
  （_PLACEHOLDER_WORDS / _RE_FUNC_START，S83 原生化后的遗留）。注册名全部不变。
"""
import os
import json
import subprocess

from tools.fs import _resolve as _fs_resolve

from registry import tool
# tools/bevy.py 自 S83 起为规则档案：bevy_rules 的正则唯一实现在 rust/src/bug.rs

MAX_FILES = 100

# ---------- Rust 薄壳（S82 起）：std_check/ui_check/bug_locate 原生实现在 rx-scan.exe ----------
# 遍历契约（名额只计代码文件/每层文件先行/upcase 序）与手写正则语义见 rust/src/scan.rs。

_RX_SCAN_EXE_NAME = "rx-scan.exe"

# 大文本不走 argv：Windows CreateProcess 命令行上限 32767 UTF-16 码元（代理对
# 最坏翻倍），10000 字符留足余量；argv 传 "-" 时 exe 侧改读 stdin 全文。
_QUERY_ARGV_CAP = 10000


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/search.py::_rx_search_exe 同纪律：候选必须是已存在且文件名恰为
    rx-scan.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_SCAN_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_SCAN_EXE_NAME:
            return c
    return None


def _rx_scan_call(argv, stdin_data=""):
    """薄壳转调 rx-scan.exe，返回结果 dict；用法级拒绝 raise ValueError。

    stdin 恒接管（空串即 EOF），子进程绝不继承宿主的协议管道。
    """
    if not stdin_data:                     # 长查询（stdin 通道）不经服务
        from tools import svc
        got = svc.json_call("scan", list(argv))
        if got is not None:
            rc, data = got
            if rc == 2:
                raise ValueError(data.get("error") if isinstance(data, dict)
                                 else str(data)[:200])
            if rc != 0:
                raise ValueError(f"rx-scan 执行失败（rc={rc}）")
            return data
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    try:
        # S90 顺带修：stdin 走二进制字节通道——text 模式会做 \n→os.linesep 换行
        # 翻译（S90 探针实锤），扫描语料需字节保真；输出按 utf-8/replace 手工解码。
        cp = subprocess.run([exe] + argv, capture_output=True, timeout=120,
                            input=(stdin_data or "").encode("utf-8"))
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan 超时（120s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # 用法级拒绝（缺参数）→ 与 fs/search 壳同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-scan 执行失败（exit={cp.returncode}）: {tail}")
    return out


# ---------- bug_scan：S83 起全量原生 ----------
# Python AST 规则（P5 作用域感知）/ Rust 生产规则 / 通用正则 / S5-C2 指纹缓存
# 已整体退役——唯一实现在 rust/src/bug.rs（rx-scan bugscan 子命令，含手写迷你
# 解析器 pyast.rs）。语义等价由 S83 对照实验证明：7 场景（语料三配额/单文件/
# 非代码/不存在路径/全仓 169 文件 909 条）与旧实现逐字节一致。
# bevy.py 保留为规则档案（bevy_rules 的正则原文在 Rust 侧 bug.rs 手写匹配器）。


@tool("bug_scan", "静态扫描 bug 模式（未定义变量/裸 except/浮点比较/eval/Rust/Bevy 等）；"
      "knowledge=true 时给每条命中附知识库条目（成因/修法/先例，S110）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
           "knowledge": {"type": "boolean",
                         "description": "附 vuln_knowledge 条目（默认 false，输出与旧版同形）"},
       },
       "required": ["path"]})
def bug_scan(path, max_files=MAX_FILES, knowledge=False):
    try:
        path = _fs_resolve(path)     # S88：S73 纪律补全——读路径同样过沙盒（code_review 同款）
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    out = _rx_scan_call(["bugscan", path, str(int(max_files))])
    if knowledge and isinstance(out, dict) and not out.get("error"):
        from tools import vulnkb
        vulnkb.annotate_issues(out)
    return out


# ---------- std_check ----------
@tool("std_check", "工程标准检查（占位文字/魔法数字/未使用导入）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录（沙盒内）"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
       },
       "required": ["path"]})
def std_check(path, max_files=MAX_FILES):
    try:
        path = _fs_resolve(path)     # S88：S73 纪律补全
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    # S83 起全域不走缓存：exe 每调独立进程，无跨调缓存面（旧 _SCAN_CACHE 已退役）
    return _rx_scan_call(["stdcheck", path, str(int(max_files))])


# ---------- ui_check：多引擎（Bevy 重点/Godot/Unity 死按钮/空容器模式） ----------
@tool("ui_check", "UI 静态检查（Bevy 重点/Godot/Unity 死按钮/空容器模式）", "scan",
      {"type": "object",
       "properties": {"path": {"type": "string", "description": "文件或目录（沙盒内）"},
                      "max_files": {"type": "integer", "description": "扫描上限（默认 100）"}},
       "required": ["path"]})
def ui_check(path, max_files=MAX_FILES):
    try:
        path = _fs_resolve(path)     # S88：S73 纪律补全
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    return _rx_scan_call(["uicheck", path, str(int(max_files))])


# ---------- bug_locate：报错 → file:line（P5：提取 traceback 文件名:行号） ----------
@tool("bug_locate", "报错文本 → 定位 file:line（含上下文片段）", "scan",
      {"type": "object",
       "properties": {
           "error_text": {"type": "string", "description": "报错/traceback 文本"},
           "root": {"type": "string", "description": "可选：搜索根目录（默认当前目录）"},
       },
       "required": ["error_text"]})
def bug_locate(error_text, root=None):
    try:
        root = _fs_resolve(root or os.getcwd())   # S88：默认 cwd 同样钳制
    except ValueError as e:
        return {"error": str(e)}
    argv = ["buglocate", root, error_text]
    stdin_data = ""
    if len(error_text) > _QUERY_ARGV_CAP:
        argv[2] = "-"
        stdin_data = error_text
    return _rx_scan_call(argv, stdin_data)


# ---------- project_scan：组合 ----------
@tool("project_scan", "项目级扫描组合：bug_scan + std_check + ui_check 三路", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
           "ui": {"type": "boolean", "description": "是否扫 UI（默认 true）"},
       },
       "required": ["path"]})
def project_scan(path, max_files=MAX_FILES, ui=True):
    try:
        path = _fs_resolve(path)     # S88：S73 纪律补全（三路子扫各自再钳，双保险）
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    bug = bug_scan(path, max_files)
    std = std_check(path, max_files)
    ui_r = ui_check(path, max_files) if ui else {"files": 0, "total": 0, "issues": []}
    return {
        "path": path,
        "bug_scan": {"total": bug.get("total", 0), "by_rule": bug.get("by_rule", {}),
                     "by_severity": bug.get("by_severity", {})},
        "std_check": {"total": std.get("total", 0)},
        "ui_check": {"total": ui_r.get("total", 0)},
        "summary": f"bug {bug.get('total', 0)} + std {std.get('total', 0)} + ui {ui_r.get('total', 0)}",
    }
