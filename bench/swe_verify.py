# -*- coding: utf-8 -*-
"""swe_verify.py —— P3 真测试执行反馈：fail-to-pass 实跑验证（S24）。

与 LLM judge 的本质区别：补丁好坏由仓库自己的测试说了算。

用法：
  python bench/swe_verify.py --pull     # parquet 拉 test_patch/FTB/PTB 合入 sample（幂等）
  python bench/swe_verify.py --envs     # uv 建 per-task venv（纯 Python 仓）
  python bench/swe_verify.py --verify   # 对 results/swe/*.json 实跑 FTB/PASS_TO_PASS
  python bench/swe_verify.py --summary  # 汇总执行验证通过率

流程（每 result 文件）：
  1) checkout 已在 base_commit；apply test_patch → 跑 FTB → 必须 FAIL（记录）
  2) apply 候选 diff（sr 协议产物）→ 跑 FTB → PASS 才算 verified
  3) 抽样跑 PASS_TO_PASS 捕回归
  4) 还原仓库（保 egg-info），幂等可重入

环境策略：每任务一个 venv（uv 秒级），install -e 任务 checkout 本体——
老 setup.py 自带 era 依赖钉，天然避开"一仓多年代"冲突。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import swe_p3

WORK = swe_p3.WORK
SAMPLE = swe_p3.SAMPLE
RESULTS_DIR = swe_p3.RESULTS_DIR
PARQUET = swe_p3.PARQUET
ENVS = os.path.join(WORK, "envs2")

# 纯 Python 仓 + 测试额外依赖（repo 本体依赖由各任务 setup.py 自己钉）
PY_REPOS = {
    "django/django": {"deps": ["tblib"], "runner": "django"},
    "sympy/sympy": {"deps": ["mpmath", "pytest<8"], "runner": "sympy"},
    "psf/requests": {"deps": ["pytest<8", "pytest-httpbin", "pytest-mock", "werkzeug"],
                     "runner": "pytest"},
    "pallets/flask": {"deps": ["pytest<8", "werkzeug==2.2.3"], "runner": "pytest"},
    "pytest-dev/pytest": {"deps": ["hypothesis", "xmlschema"], "runner": "pytest"},
    "sphinx-doc/sphinx": {"deps": ["pytest<8", "setuptools<81"], "runner": "pytest"},
    "pylint-dev/pylint": {"deps": ["pytest<8"], "runner": "pytest"},
}
# 个别任务的年代钉（一仓多任务跨年代时的特例）
TASK_DEPS = {
    "pylint-dev__pylint-4661": ["pytest<8", "wrapt==1.12.1", "astroid==2.6.6"],
    "psf__requests-1142": ["pytest<8", "pytest-mock",
                           "urllib3<1.25", "chardet", "idna"],
    "psf__requests-1766": ["pytest<8", "pytest-mock",
                           "urllib3<1.25", "chardet", "idna"],
    "psf__requests-1921": ["pytest<8", "pytest-mock",
                           "urllib3<1.25", "chardet", "idna"],
}
# 需要更低 Python 的任务（wrapt<1.13 依赖 py3.10 才有的 inspect.formatargspec）
TASK_PY = {"pylint-dev__pylint-4661": "3.10"}
PTB_CAP = 25
TEST_TIMEOUT = 900

# S28：C 扩展仓走 WSL（Ubuntu 24.04 + uv 托管 python + gcc 现场构建）。
# 每任务独立 venv（WSL ~/swe/envs2/<iid>），era 依赖钉 + pip legacy develop。
WSL_MOUNT = "/mnt/c/Users/lbx13/AppData/Local/Temp/opencode/swe"
WSL_TASKS = {
    "scikit-learn__scikit-learn-10908": {"py": "3.8"},
    "scikit-learn__scikit-learn-11310": {"py": "3.8"},
    "scikit-learn__scikit-learn-12973": {"py": "3.7",
        "interp": "$HOME/swe/py37/bin/python3"},   # vendored cloudpickle CodeType 3.8 移除
    "scikit-learn__scikit-learn-13142": {"py": "3.7",
        "interp": "$HOME/swe/py37/bin/python3"},
    "scikit-learn__scikit-learn-14629": {"py": "3.8"},
    "scikit-learn__scikit-learn-14894": {"py": "3.8"},
    "scikit-learn__scikit-learn-26323": {"py": "3.10"},
    "matplotlib__matplotlib-22865": {"py": "3.8"},
    "matplotlib__matplotlib-24026": {"py": "3.8"},
    "matplotlib__matplotlib-24149": {"py": "3.8"},
    "matplotlib__matplotlib-25287": {"py": "3.10"},
    "astropy__astropy-14539": {"py": "3.8"},
    "pydata__xarray-4356": {"py": "3.8"},
    "pydata__xarray-7229": {"py": "3.8"},
    "mwaskom__seaborn-3069": {"py": "3.8"},
    "mwaskom__seaborn-3187": {"py": "3.8"},
}
_OLD = ["setuptools<60", "wheel", "cython==0.29.36", "numpy==1.17.3",
        "scipy==1.4.1", "joblib", "threadpoolctl", "pytest<8"]
_NEW = ["setuptools<70", "wheel", "cython==0.29.36", "numpy==1.23.5",
        "scipy==1.10.1", "joblib", "threadpoolctl", "pytest<8"]
_MPL = ["setuptools<60", "wheel", "numpy==1.17.3", "pillow", "pytest<8",
        "pyparsing", "setuptools_scm<6"]
_XR = ["setuptools<60", "wheel", "numpy==1.17.3", "pandas==0.24.2",
       "pytest<8"]
_SB = ["setuptools<60", "wheel", "flit_core", "numpy==1.23.5",
       "pandas==1.4.4", "matplotlib==3.5.1", "pytest<8"]
_SB2 = ["wheel", "flit_core", "numpy==1.17.3", "pandas==0.25.3",
        "matplotlib==3.2.1", "pytest<8", "setuptools<60"]
_AST = ["setuptools<60", "wheel", "cython==0.29.36", "numpy==1.17.3",
        "pytest<8", "extension_helpers<1", "setuptools_scm<6"]
for _iid, _t in WSL_TASKS.items():
    if _iid.startswith("matplotlib"):
        _t["deps"] = (_NEW + ["pybind11", "certifi"]) if "25287" in _iid else _MPL
        _t["pkg"] = "matplotlib"
        _t["scmver"] = {"22865": "3.0.2", "24026": "3.0.3", "24149": "3.1.0",
                        "25287": "3.7.1"}[_iid.split("-")[-1]]
    elif _iid.startswith("astropy"):
        _t["deps"] = _AST
        _t["pkg"] = "astropy"
        _t["scmver"] = "3.1.0"
        _t["py"] = "3.8"          # astropy 14539 元数据声明 py>=3.8
    elif _iid.startswith("pydata"):
        _t["deps"] = _XR
        _t["pkg"] = "xarray"
    elif _iid.startswith("mwaskom"):
        _t["deps"] = _SB2 if "3187" in _iid else _SB
        _t["pkg"] = "seaborn"
        _t["scmver"] = "0.10.0" if "3187" in _iid else "0.9.0"
    else:
        _t["deps"] = _NEW if _t["py"] == "3.10" else _OLD
        _t["pkg"] = "sklearn"


_WSL_SEQ = [0]


def wsl_run(script):
    """把 bash 脚本写到 Windows 侧临时文件，经 /mnt/c 执行（免引号地狱）。
    S29：名字含 pid+序号——hash 碰撞会让并发构建执行错脚本。"""
    _WSL_SEQ[0] += 1
    d = os.path.join(tempfile.gettempdir(), "opencode")
    os.makedirs(d, exist_ok=True)             # S29 模糊：pytest 改 TMPDIR 后目录可能不存在
    p = os.path.join(d, f"wsl_{os.getpid()}_{_WSL_SEQ[0]}.sh")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    mount = p.replace("\\", "/").replace("C:/", "/mnt/c/")
    env = dict(os.environ)
    env.setdefault("MPLBACKEND", "Agg")       # WSL 无显示：seaborn import mpl 会拉 backend
    r = subprocess.run(["wsl", "-e", "bash", mount],
                       capture_output=True, timeout=TEST_TIMEOUT + 600, env=env)
    out = (r.stdout or b"").decode(errors="replace")
    err = (r.stderr or b"").decode(errors="replace")
    return r.returncode, out, err


def build_env_wsl(inst):
    iid = inst["instance_id"]
    cfg = WSL_TASKS[iid]
    pyver = cfg["py"]
    deps = cfg["deps"]
    pkg = cfg.get("pkg", "sklearn")
    interp = cfg.get("interp")
    scmver = cfg.get("scmver", "")
    co = f"{WSL_MOUNT}/{safe_iid(iid)}"
    safe = safe_iid(iid)
    depq = " ".join(f"'{d}'" for d in deps)
    scm = cfg.get("scmver", "")
    scm_line = (f'export SETUPTOOLS_SCM_PRETEND_VERSION={scm}\n'
                f'export MPLBACKEND=Agg') if scm else 'export MPLBACKEND=Agg'
    if interp:
        # py3.7 路径：stdlib venv + pip 链（uv 已不支持 3.7；pip 钉 <24.1）
        script = f"""#!/usr/bin/env bash
set -e
E=~/swe/envs2/{safe}
V=$E/bin/python
CO={co}
export MPLBACKEND=Agg
[ -x "$V" ] && "$V" -c "import {pkg}; import pytest" 2>/dev/null && {{ echo "{pkg} OK"; exit 0; }}
rm -rf "$E"
{interp} -m venv "$E" 2>&1 | tail -1
"$V" -m pip install -q --upgrade "pip<24.1" "setuptools<60" wheel 2>&1 | tail -1
"$V" -m pip install -q {depq} 2>&1 | tail -1
{scm_line}
"$V" -m pip install --no-build-isolation -e "$CO" 2>&1 | tail -2
"$V" -c "import {pkg}; print('{pkg} OK')"
"""
    else:
        # py3.8/3.10 路径：uv 秒装依赖，构建走 venv pip legacy develop
        script = f"""#!/usr/bin/env bash
set -e
E=~/swe/envs2/{safe}
V=$E/bin/python
UV=~/.local/bin/uv
CO={co}
export MPLBACKEND=Agg
[ -x "$V" ] && "$V" -c "import {pkg}; import pytest" 2>/dev/null && {{ echo "{pkg} OK"; exit 0; }}
rm -rf "$E"
"$UV" venv --seed --python {pyver} "$E" 2>&1 | tail -1
"$UV" pip install --python "$V" {depq} 2>&1 | tail -1
{scm_line}
"$V" -m pip install --no-build-isolation -e "$CO" 2>&1 | tail -2
if [ "$pkg" = "matplotlib" ] && [ ! -f "$CO/lib/matplotlib/_version.py" ]; then
  printf '__version__ = version = "{scmver}"\n__version_tuple__ = version_tuple = (3, 9, 0)\n' > "$CO/lib/matplotlib/_version.py"
fi
"$V" -c "import {pkg}; print('{pkg} OK')"
"""
    rc, out, err = wsl_run(script)
    ok = "OK" in out
    if not ok:
        log(f"[ENV-FAIL/wsl] {iid}: {(err or out)[-300:]}")
    return ok


def _run_tests_wsl(inst, iid, ftb):
    if not ftb:
        return None, "no-ftb"
    import shlex
    co = f"{WSL_MOUNT}/{safe_iid(iid)}"
    V = f"~/swe/envs2/{safe_iid(iid)}/bin/python"
    ids = " ".join(shlex.quote(x) for x in ftb)      # S29：node id 不可信
    script = f"""#!/usr/bin/env bash
# S42 修复：V 此前只是 Python 变量，bash 里从未定义 → "$V" 空命令，测试从未真跑
cd {co}
V={V}
"$V" -m pytest -q --no-header -p no:cacheprovider --continue-on-collection-errors {ids} 2>&1 | tail -20
"""
    rc, out, err = wsl_run(script)
    tail = (out or err)[-1200:]
    return rc, tail


def safe_iid(iid):
    """instance_id → 文件系统安全名（S29：语料可控输入不进路径穿越）。"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "__", str(iid or ""))
    s = s.replace("..", "__")                 # 穿越点清零
    return s[:120].strip("._-") or "unnamed"


def log(msg):
    print(msg, flush=True)


if hasattr(sys.stdout, "reconfigure"):          # GBK 控制台兼容（uv 报错含非 GBK 字符）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _uv_py(env_dir):
    return os.path.join(env_dir, "Scripts", "python.exe")


def pull():
    import duckdb
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT instance_id, test_patch, FAIL_TO_PASS, PASS_TO_PASS "
        f"FROM read_parquet('{PARQUET}')").fetchall()
    meta = {r[0]: r for r in rows}
    out = []
    added = 0
    for line in open(SAMPLE, encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        m = meta.get(d["instance_id"])
        if m and "test_patch" not in d:
            d["test_patch"] = m[1] or ""
            d["ftb"] = json.loads(m[2]) if m[2] else []
            d["ptb"] = json.loads(m[3]) if m[3] else []
            added += 1
        out.append(d)
    with open(SAMPLE, "w", encoding="utf-8") as f:
        for d in out:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    try:
        loc = os.path.relpath(SAMPLE, ROOT)
    except ValueError:                     # 跨盘符（测试 tmp_path 场景）
        loc = SAMPLE
    log(f"[OK] pull 完成：新增字段 {added} 条 -> {loc}")


def _run(cmd, cwd, timeout=TEST_TIMEOUT, env=None):
    e = dict(os.environ)
    e.update(env or {})
    e.setdefault("MPLBACKEND", "Agg")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=e,
                           text=True, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "TIMEOUT"


def build_env_task(inst):
    """每任务一个 venv：install -e 任务 checkout（base 态）+ 测试依赖。"""
    iid = inst["instance_id"]
    dst = os.path.join(ENVS, iid.replace("/", "__"))
    py = _uv_py(dst)
    if os.path.exists(py):
        return py
    checkout = os.path.join(WORK, iid.replace("/", "__"))
    if not os.path.isdir(checkout):
        return None
    os.makedirs(ENVS, exist_ok=True)
    deps = TASK_DEPS.get(iid) or PY_REPOS[inst["repo"]]["deps"]
    pretend = {"SETUPTOOLS_SCM_PRETEND_VERSION": str(inst.get("version") or "1.0")}
    last_err = ""
    if iid in TASK_PY:
        vers = [TASK_PY[iid]]
    else:
        vers = ["3.11", "3.8"]                # 上古仓 collections.Mapping 需要 3.8
    for ver in vers:
        r = subprocess.run(["uv", "venv", "--seed", dst, "--python", ver],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
        if r.returncode != 0:
            last_err = (r.stderr or "")[:150]
            shutil.rmtree(dst, ignore_errors=True)
            continue
        r = subprocess.run(["uv", "pip", "install", "--python", py, "-e", checkout]
                           + deps, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800,
                           env={**os.environ, **pretend})
        if r.returncode == 0:
            log(f"[ENV-OK] {iid} (py{ver})")
            return py
        last_err = (r.stderr or "")[:600]
        # 上古 setup.py 在新 setuptools 下炸 → 退回 venv pip --no-build-isolation
        r2 = subprocess.run(
            [py, "-m", "pip", "install", "-e", checkout, "--no-build-isolation",
             "--quiet"] + deps,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800,
            env={**os.environ, **pretend, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        if r2.returncode == 0:
            log(f"[ENV-OK] {iid} (py{ver}, pip)")
            return py
        last_err += " | pip-fallback: " + (r2.stderr or "")[:400]
        shutil.rmtree(dst, ignore_errors=True)            # 换版本重建
    log(f"[ENV-FAIL] {iid}: {last_err}")
    return None


def envs():
    import concurrent.futures as cf
    tasks = [d for d in swe_p3.load_sample() if d["repo"] in PY_REPOS]
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(build_env_task, tasks))
    ok = sum(1 for x in res if x)
    log(f"[OK] envs ready {ok}/{len(tasks)}")


def _django_labels(raw):
    out = []
    for lbl in raw:
        m = re.match(r"^(\S+) \((.+)\)$", lbl)
        if not m:
            return []                      # 无法解析（如整句话）→ 不可跑
        name, path = m.group(1), m.group(2)
        if path.endswith("." + name):                # 去掉重复尾部的 test 名
            path = path[:-(len(name) + 1)]
        out.append(f"{path}.{name}")
    return out


def _sympy_resolve(root, names):
    """裸 test 名 → 所在测试文件列表（def <name>( 全仓搜索，限 tests 目录优先）。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "doc", "docs", "examples", "build", ".pybuild")]
        for fn in filenames:
            if not fn.startswith("test") or not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            if any(f"def {n}(" in txt for n in names):
                hits.append(os.path.relpath(fp, root).replace("/", os.sep))
    return hits[:20]


def _run_tests(inst, py, root, ftb, timeout=TEST_TIMEOUT):
    if inst["instance_id"] in WSL_TASKS:          # S28：C 扩展仓走 WSL
        return _run_tests_wsl(inst, inst["instance_id"], ftb)
    runner = PY_REPOS[inst["repo"]]["runner"]
    if runner == "django":
        labels = _django_labels(ftb)
        if not labels:
            return None, "unparseable-ftb"
        cmd = [py, os.path.join("tests", "runtests.py"), "--verbosity", "1",
               "--parallel", "1"] + labels
        rc, out, err = _run(cmd, cwd=root, timeout=timeout)
    elif runner == "sympy":
        files = _sympy_resolve(root, [re.sub(r"[\[\].*]", "", x) for x in ftb])
        if not files:
            return None, "ftb-not-located"
        ks = " or ".join(ftb)
        cmd = [py, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
               "-k", ks] + files
        rc, out, err = _run(cmd, cwd=root, timeout=timeout)
    else:
        cmd = [py, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
               "--continue-on-collection-errors"] + ftb
        rc, out, err = _run(cmd, cwd=root, timeout=timeout)
    tail = ((out or "")[-1600:] + "\n===STDERR===\n" + (err or "")[-700:]) if rc \
        else (out or "")[-500:]
    return rc, tail


def _restore(root):
    _run(["git", "-C", root, "checkout", "--", "."], cwd=root, timeout=120)
    _run(["git", "-C", root, "clean", "-fdq", "-e", "*.egg-info", "-e", "__pycache__",
          "-e", ".eggs", "-e", "*.egg-link", "-e", ".pytest_cache"], cwd=root, timeout=120)


def verify_one(rec, inst, py, cache):
    iid = rec["instance_id"]
    root = os.path.join(WORK, iid.replace("/", "__"))
    if not os.path.isdir(root):
        return {"skip": "no-checkout"}
    if not (inst.get("test_patch") and inst.get("ftb")):
        return {"skip": "no-test-patch"}
    v = {}
    _restore(root)
    tp = inst["test_patch"].replace("\r\n", "\n")
    r = subprocess.run(["git", "-C", root, "apply", "--whitespace=nowarn", "-"],
                       input=tp.encode(), capture_output=True, timeout=120)
    if r.returncode != 0:
        return {"skip": f"test-patch-apply-failed: {r.stderr[:120]}"}
    ftb = list(inst.get("ftb") or [])
    base_key = iid + "::base"
    if base_key in cache:
        v["base_ftb_fail"] = cache[base_key]
    else:
        rc, tail = _run_tests(inst, py, root, ftb)
        if rc is None:
            return {"skip": tail}
        v["base_ftb_fail"] = (rc != 0)
        v["base_tail"] = tail[-400:]
        cache[base_key] = v["base_ftb_fail"]
    cand = (rec.get("mech") or {}).get("candidate_diff") or ""
    if not cand.strip():
        v["verified"] = False
        v["why"] = "no-candidate-diff"
        return v
    r = subprocess.run(["git", "-C", root, "apply", "--whitespace=nowarn", "-"],
                       input=cand.replace("\r\n", "\n").encode(), capture_output=True,
                       timeout=120)
    if r.returncode != 0:
        v["verified"] = False
        v["why"] = f"candidate-apply-failed: {r.stderr[:150]}"
        return v
    rc, tail = _run_tests(inst, py, root, ftb)
    v["ftb_pass"] = (rc == 0)
    v["ftb_tail"] = tail[-600:]
    ptb = (inst.get("ptb") or [])
    if ptb and v["ftb_pass"]:
        sample = ptb[:PTB_CAP]
        rc2, _ = _run_tests(inst, py, root, sample)
        v["ptb_total"] = len(sample)
        v["ptb_pass"] = (rc2 == 0)
    v["verified"] = bool(v.get("ftb_pass") and v.get("ptb_pass", True))
    return v


def verify(args):
    insts = {d["instance_id"]: d for d in swe_p3.load_sample()}
    cache = {}
    import glob
    files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*_A.json")) +
                   glob.glob(os.path.join(RESULTS_DIR, "*_B.json")))
    if args.ids:
        want = {x.strip() for x in args.ids.split(",")}
        files = [f for f in files if json.load(open(f, encoding="utf-8"))
                 ["instance_id"] in want]
    done = 0
    for fp in files:
        rec = json.load(open(fp, encoding="utf-8"))
        if "verify" in rec and not args.force:
            continue
        inst = insts.get(rec["instance_id"])
        if inst is None:
            continue
        if rec["instance_id"] in WSL_TASKS:        # WSL 环境按需构建（幂等）
            if not build_env_wsl(inst):
                rec["verify"] = {"skip": "no-env-wsl"}
                json.dump(rec, open(fp, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                continue
            t0 = time.time()
            rec["verify"] = verify_one(rec, inst, None, cache)
            log(f"verify {os.path.basename(fp)} -> "
                f"{rec['verify'].get('verified', rec['verify'].get('skip'))} "
                f"({time.time()-t0:.0f}s)")
            json.dump(rec, open(fp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            done += 1
            continue
        py = _uv_py(os.path.join(ENVS, rec["instance_id"].replace("/", "__")))
        if not os.path.exists(py):
            rec["verify"] = {"skip": "no-env"}
        else:
            t0 = time.time()
            rec["verify"] = verify_one(rec, inst, py, cache)
            log(f"verify {os.path.basename(fp)} -> "
                f"{rec['verify'].get('verified', rec['verify'].get('skip'))} "
                f"({time.time()-t0:.0f}s)")
        json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        done += 1
    log(f"[OK] verify done {done} files")


def summary():
    import glob
    agg = {}
    for fp in sorted(glob.glob(os.path.join(RESULTS_DIR, "*_*.json"))):
        if os.path.basename(fp) == "summary.json":
            continue
        d = json.load(open(fp, encoding="utf-8"))
        v = d.get("verify") or {}
        a = agg.setdefault(d["arm"], {"n": 0, "feasible": 0, "verified": 0,
                                      "base_bad": 0})
        a["n"] += 1
        if "skip" not in v:
            a["feasible"] += 1
            if v.get("base_ftb_fail") is False:
                a["base_bad"] += 1
            a["verified"] += int(v.get("verified") is True)
    print(f"{'arm':<4}{'n':>4}{'feasible':>10}{'verified':>10}{'verified%':>11}{'base_bad':>10}")
    for name, s in sorted(agg.items()):
        f = max(s["feasible"], 1)
        print(f"{name:<4}{s['n']:>4}{s['feasible']:>10}{s['verified']:>10}"
              f"{s['verified']/f*100:>10.1f}%{s['base_bad']:>10}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--envs", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ids", default="")
    a = ap.parse_args()
    if a.pull:
        pull()
    if a.envs:
        envs()
    if a.verify:
        verify(a)
    if a.summary:
        summary()
    if not any((a.pull, a.envs, a.verify, a.summary)):
        print(__doc__)


if __name__ == "__main__":
    main()
