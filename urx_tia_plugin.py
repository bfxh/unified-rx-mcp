# -*- coding: utf-8 -*-
"""urx_tia_plugin.py —— pytest 插件（S104 TIA）：记录测试的文件依赖。

由 tools/ide_test.py 以 `-p urx_tia_plugin` 注入（PYTHONPATH 指向本仓根）。
零依赖：只用 stdlib 的 audit hook（sys.addaudithook），不改测试代码。

记录分两层（实锤教训：测试模块在**收集阶段**就被 import，执行期不再 open——
只记执行期会得到空依赖图）：
- `files`：收集期按"正在收集的文件"归集的打开（测试模块与其 import 的模块）；
- `tests`：执行期（setup/call/teardown）按当前 nodeid 归集的打开。
调用方合并：test 的依赖 = tests[nodeid] ∪ files[nodeid 的所属文件]。

输出：sessionfinish 时向 `sys.__stdout__` 打一行
`URX_TIA_JSON {json}`（调用方用 `pytest -s` 运行以绕过输出捕获）——
**不做任何文件 I/O**（路径纪律：不给外部输入拼路径的机会）。
"""
import json
import os
import sys

MARKER = "URX_TIA_JSON "
_EXTS = {".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".gd", ".cs",
         ".dart", ".lua", ".java", ".kt", ".rb", ".php", ".swift", ".c",
         ".cpp", ".h", ".hpp", ".sh", ".toml", ".yaml", ".yml", ".md",
         ".json", ".txt", ".ini", ".cfg", ".sql", ".vue", ".svelte"}

_root = ""
_current = None       # 当前执行的测试 nodeid
_collecting = None    # 当前收集的文件（相对 root）
_test_deps = {}
_file_deps = {}


def _record(bucket, key):
    if key is not None:
        bucket.setdefault(key, set())


def _norm(p):
    """`.pyc` → 源文件路径（实锤：模块二次导入走 __pycache__，只认 .py 会漏光）；
    映射失败返回 None。"""
    if not p.endswith(".pyc"):
        return p
    base = os.path.basename(p)
    stem = base.split(".")[0]
    d = os.path.dirname(os.path.dirname(p))       # 去掉 __pycache__ 层
    cand = os.path.join(d, stem + ".py")
    return cand if os.path.isfile(cand) else None


def _audit(event, args):
    if event != "open" or not args:
        return
    if _current is None and _collecting is None:
        return
    p = args[0]
    try:
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        elif not isinstance(p, str):
            p = os.fspath(p)
    except (TypeError, ValueError):
        return
    p = _norm(p)
    if not p or os.path.splitext(p)[1].lower() not in _EXTS:
        return
    try:
        ap = os.path.abspath(p)
        if not ap.lower().startswith(_root.lower()):
            return
        # 统一正斜杠：与 pytest nodeid 的路径写法一致（Windows 下 relpath 是反斜杠）
        rel = os.path.relpath(ap, _root).replace("\\", "/")
    except (OSError, ValueError):
        return
    if _current is not None:
        _record(_test_deps, _current)
        _test_deps[_current].add(rel)
    elif _collecting is not None:
        _record(_file_deps, _collecting)
        _file_deps[_collecting].add(rel)


def pytest_sessionstart(session):
    global _root
    _root = os.path.abspath(os.environ.get("URX_TIA_ROOT") or os.getcwd())
    sys.addaudithook(_audit)


def pytest_collectstart(collector):
    global _collecting
    p = getattr(collector, "path", None)
    try:
        _collecting = (os.path.relpath(os.path.abspath(str(p)), _root).replace("\\", "/")
                       if p else None)
    except (OSError, ValueError):
        _collecting = None


def pytest_collectreport(report):
    global _collecting
    _collecting = None


def pytest_runtest_setup(item):
    global _current
    _current = item.nodeid
    _record(_test_deps, _current)


def pytest_runtest_teardown(item, nextitem):
    global _current
    _current = None


def pytest_sessionfinish(session, exitstatus):
    data = {"tests": {k: sorted(v) for k, v in _test_deps.items()},
            "files": {k: sorted(v) for k, v in _file_deps.items()}}
    try:
        sys.__stdout__.write("\n" + MARKER + json.dumps(data, ensure_ascii=False) + "\n")
        sys.__stdout__.flush()
    except (OSError, ValueError):
        pass
