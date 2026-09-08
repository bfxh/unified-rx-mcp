# -*- coding: utf-8 -*-
"""tools/ide_test.py —— 统一测试入口（R2）：pytest / cargo test / go test。

一条命令 → per-test 结构化结果 + 失败帧（解析器复用 ide_debug，不另造轮子）。
诚实点：
- pytest 收集到 0 个测试（exit 5）→ 显式报出——"没写测试"是工具输出里的事实，
  不是静默成功
- go 不加 -v 不报通过数 → 用 -v 数 --- PASS/FAIL/SKIP 行
- 修复杂轮回喂已有 S37 通道（pytest 帧已在修复提示词），本工具不重复接线

回喂计划如实修订：规划时说"接 swe_repair"，证据核对后 S37 已接（bench/swe_repair
Structured pytest 段），重复接线 = S47 已否决的信号堆通道，故砍。
"""
import os
import re
import subprocess
import sys

from registry import tool
from tools import cache as _cache
from tools import tia as _tia
from tools.fs import _resolve as _fs_resolve
from tools.ide_debug import _parse_pytest, _parse_rust_panic

_RE_PYTEST_SUM = re.compile(
    r"(?:=\+\s*)?(?:(\d+) failed)?(?:.*?(\d+) passed)?(?:.*?(\d+) skipped)?"
    r"(?:.*?(\d+) error)?\s*(?:in\s+[\d.]+s)?\s*(?:=\+\s*)?$")
_RE_PYTEST_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", re.M)
_RE_CARGO_TEST_LINE = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)\s*$", re.M)
_RE_CARGO_RESULT = re.compile(
    r"test result:\s*(\w+)\.\s*(\d+) passed; (\d+) failed; (\d+) ignored")
_RE_GO_TEST_LINE = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)", re.M)


def _which_py():
    return sys.executable or "python"


def _detect(path):
    """项目测试设施探测：cargo > go > pytest。返回 (kind, build_root)。"""
    if os.path.isfile(os.path.join(path, "Cargo.toml")):
        return "cargo", path
    if os.path.isfile(os.path.join(path, "go.mod")):
        return "go", path
    # python：tests 目录 / test_*.py / pytest 配置，任一即认
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "node_modules", "target", "__pycache__", "dist",
                    "build", ".venv", "venv", "backups")]
        if r.endswith(("test", "tests")) or "pytest" in os.path.basename(r):
            for fn in fs:
                if fn.startswith("test_") and fn.endswith(".py"):
                    return "pytest", path
        for fn in fs:
            if fn in ("pytest.ini", "pyproject.toml", "setup.cfg") or \
               (fn.startswith("test_") and fn.endswith(".py")):
                return "pytest", path
    return None, path


@tool("ide_test", "统一测试入口：pytest / cargo test / go test 一条命令 → "
      "per-test 结构化结果 + 失败帧；收集到 0 个测试时显式报出（没写测试也是事实）。"
      "tia=true 时做测试影响分析（仅 pytest）：只跑受文件变更影响的测试",
      "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（沙盒内）"},
           "target": {"type": "string",
                      "description": "可选：pytest 测试路径/关键字、cargo 过滤词、go -run 正则"},
           "timeout": {"type": "integer", "description": "秒（默认 600）"},
           "tia": {"type": "boolean",
                   "description": "测试影响分析（仅 pytest）：按变更文件选跑，首次全量建依赖图"},
           "full": {"type": "boolean",
                    "description": "tia 模式下强制全量（重建依赖图，不选跑）"},
       },
       "required": ["path"]},
      requires_auth=True)
def ide_test(path, target=None, timeout=600, tia=False, full=False):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    # S60：target 防 argv 注入——旗标串进 pytest/cargo 命令行会改变执行语义
    if target and target.startswith("-"):
        return {"error": "target 不接受 '-' 旗标（防 argv 注入）——"
                         "只收测试路径/名字/正则"}
    kind, root = _detect(path)
    if kind is None:
        return {"error": "未检测到测试设施（Cargo.toml / go.mod / pytest 配置或 test_*.py）"}
    if kind == "pytest":
        return _run_pytest(path, root, target, timeout, tia=tia, full=full)
    res = {"cargo": _run_cargo, "go": _run_go}[kind](path, root, target, timeout)
    if tia:
        res["tia"] = {"mode": "unsupported",
                      "note": f"TIA 目前仅支持 pytest（本目标为 {kind}），已全量执行"}
    return res


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _collect_pytest(path, target, timeout):
    """`--collect-only -q` 取本次 nodeid 列表；失败/含非 nodeid 行 → None（退回全量）。"""
    r = _exec([_which_py(), "-m", "pytest", "--collect-only", "-q"]
              + ([target] if target else []), path, timeout)
    if r.get("error"):
        return None
    out = (r["stdout"] or "")
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("=", "!", "no tests", "error")):
            continue
        if "::" in line:
            ids.append(line)
        elif line.endswith(".py"):
            return None          # 文件级条目：无法按测试选择 → 退回全量
    return ids or None


def _base_result(kind, exit_code):
    return {"tool": kind, "exit": exit_code, "passed": None, "failed": None,
            "skipped": None, "failures": []}


def _run_pytest(path, root, target, timeout, tia=False, full=False):
    tia_info, deps_prev, args = None, {}, []
    if tia:
        prev = _tia.get(root)
        snap0 = _cache.snapshot(root)
        if prev and not full and snap0 is not None:
            collected = _collect_pytest(path, target, timeout)
            if collected:
                chg = _tia.changed_files(prev.get("snapshot"), snap0)
                sel, stats = _tia.select(collected, prev.get("deps"), chg)
                if stats["selected"] == 0:
                    return {"tool": "pytest", "exit": 0, "passed": 0, "failed": 0,
                            "skipped": 0, "collected": 0, "failures": [],
                            "tia": {"mode": "skipped-no-impact", **stats,
                                    "note": "无受影响测试，本次未执行（全量用 full=true）"}}
                deps_prev = prev.get("deps") or {}
                if stats["selected"] < stats["collected"]:
                    args = sel
                    tia_info = {"mode": "incremental", **stats}
                else:
                    tia_info = {"mode": "full-all-affected", **stats,
                                "note": "全部测试都受影响，按全量跑"}
            else:
                tia_info = {"mode": "full-collect-failed",
                            "note": "收集失败/无法按测试选择，退回全量"}
        else:
            tia_info = {"mode": "full-first" if not full else "full-forced",
                        "note": "首次或强制：全量执行并建立/重建依赖图"}
    cmd = [_which_py(), "-m", "pytest", "-q", "-rfE", "--tb=short"]
    env_extra = {}
    if tia:
        # -s 绕过输出捕获，让插件的依赖图标记行直达 stdout；插件经 PYTHONPATH 注入
        cmd += ["-s", "-p", "urx_tia_plugin"]
        env_extra = {"PYTHONPATH": _repo_root() + os.pathsep
                     + os.environ.get("PYTHONPATH", ""),
                     "URX_TIA_ROOT": root}
    r = _exec(cmd + ([target] if (target and not args) else []) + args,
              path, timeout, env_extra=env_extra)
    if r.get("error"):
        return r
    out = (r["stdout"] or "") + "\n" + (r["stderr"] or "")
    res = _base_result("pytest", r["code"])
    if r["code"] == 5:
        res.update({"collected": 0,
                    "note": "pytest 收集到 0 个测试（exit 5）——没写测试/全被过滤，"
                            "这个事实本身就是要处理的问题"})
        return res
    if "No module named" in out and "pytest" in out:
        return {"error": f"pytest 不可用（解释器 {_which_py()}）——请安装或换解释器"}
    failed_ids, asserts = _parse_pytest(out)
    # 摘要行（-q 模式无 ==== 包裹）：取最后一个 "N passed, M failed ... in Xs"
    summ = ""
    for mm in re.finditer(
            r"((?:\d+ (?:passed|failed|errors?|skipped)(?:,\s*)?)+) in\s+[\d.]+s", out):
        summ = mm.group(1)
    def _num(word):
        mm = re.search(rf"(\d+) {word}", summ)
        return int(mm.group(1)) if mm else 0
    res.update({"passed": _num("passed"),
                "failed": _num("failed") + _num("errors"),  # 收集错误也是失败
                "skipped": _num("skipped"), "collected":
                _num("passed") + _num("failed") + _num("skipped") + _num("errors")})
    for m2 in _RE_PYTEST_FAILED_LINE.finditer(out):
        res["failures"].append({"test": m2.group(1), "msg": (m2.group(2) or "")[:200]})
    for fid in failed_ids:
        if not any(f["test"].endswith(fid) or fid in f["test"]
                   for f in res["failures"]):
            res["failures"].append({"test": fid, "msg": ""})
    for f in res["failures"][:3]:
        if not f["msg"] and asserts:
            f["msg"] = asserts[0]
    res["note"] = "失败帧已可直喂修复轮（pytest -rf + E 断言行）"
    if res["failures"]:
        res["retry_hint"] = ("ide_test(path=..., target='"
                             + res["failures"][0]["test"] + "')")
    if tia:
        recorded = _parse_tia_marker(out)
        # 合并两层依赖：执行期（tests[nodeid]）∪ 收集期（files[所属文件]）
        file_deps = recorded.get("files") or {}
        test_deps = recorded.get("tests") or {}
        merged_new = {}
        for nodeid, files in test_deps.items():
            f = nodeid.split("::")[0]
            merged_new[nodeid] = sorted(set(files) | set(file_deps.get(f) or []))
        # 本次未执行的测试保留上次依赖（保守）
        merged = {**deps_prev, **merged_new}
        snap1 = _cache.snapshot(root)
        if snap1 is not None:
            _tia.put(root, snap1, merged)
        res["tia"] = {**(tia_info or {}), "recorded_tests": len(merged_new),
                      "deps_total": len(merged)}
    return res


def _parse_tia_marker(out):
    """从 pytest 输出里取插件打的依赖图标记行；缺失返回 {}。"""
    data = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("URX_TIA_JSON "):
            try:
                import json as _json
                obj = _json.loads(line[len("URX_TIA_JSON "):])
                if isinstance(obj, dict):
                    data = obj
            except ValueError:
                continue
    return data


def _run_cargo(path, root, target, timeout):
    r = _exec(["cargo", "test"] + ([target] if target else []), path, timeout)
    if r.get("error"):
        return r
    out = (r["stdout"] or "") + "\n" + (r["stderr"] or "")
    res = _base_result("cargo", r["code"])
    # S63：workspace 多 crate → 多条 "test result:" 行，全量累加
    # （此前只取第一条，VF3 实测 177 测试只报 52）
    passed = failed = ignored = 0
    seen = 0
    for m in _RE_CARGO_RESULT.finditer(out):
        seen += 1
        passed += int(m.group(2))
        failed += int(m.group(3))
        ignored += int(m.group(4))
    if seen:
        res.update({"passed": passed, "failed": failed, "skipped": ignored,
                    "collected": passed + failed + ignored,
                    "result_lines": seen})
    for m2 in _RE_CARGO_TEST_LINE.finditer(out):
        if m2.group(2) == "FAILED":
            res["failures"].append({"test": m2.group(1), "msg": ""})
    panics = _parse_rust_panic(out)
    if panics:
        p0 = panics[0]
        res["panic"] = p0.get("msg", "")
        res["panic_at"] = f"{p0.get('file')}:{p0.get('line')}"
        res["frames"] = p0.get("backtrace") or []
    if res["failures"]:
        res["retry_hint"] = ("ide_test(path=..., target='"
                             + res["failures"][0]["test"] + "')")
    return res


def _run_go(path, root, target, timeout):
    r = _exec(["go", "test", "-v"] +
              (["-run", target] if target else []) + ["./..."], path, timeout)
    if r.get("error"):
        return r
    out = (r["stdout"] or "") + "\n" + (r["stderr"] or "")
    res = _base_result("go", r["code"])
    n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for m in _RE_GO_TEST_LINE.finditer(out):
        n[m.group(1)] += 1
        if m.group(1) == "FAIL":
            res["failures"].append({"test": m.group(2), "msg": ""})
    res.update({"passed": n["PASS"], "failed": n["FAIL"], "skipped": n["SKIP"],
                "collected": sum(n.values())})
    return res


def _exec(cmd, cwd, timeout, env_extra=None):
    env = {**os.environ, "PYTHONUTF8": "1"}
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           text=True, encoding="utf-8", errors="replace",
                           env=env)
    except FileNotFoundError:
        return {"error": f"可执行文件不存在: {cmd[0]}"}
    except subprocess.TimeoutExpired:
        return {"error": f"超时（{timeout}s）"}
    return {"code": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}
