"""常驻服务客户端（S151）：把"每次调用 ~9ms 的进程创建"降到 ~0.1ms。

架构（首版 TCP 环回被本机安全栈间歇拦截——min 0.4ms/中位 15ms——弃用）：
宿主起一个 `rx-svc.exe serve` 常驻子进程，请求/应答各一行 JSON 走它的
stdin/stdout。**无监听端口、无令牌、无端口文件**——只有本进程能写它的 stdin；
宿主退出（或本模块 atexit 关管道）→ 子进程 EOF 自退，无孤儿。

**质量不变**（硬约束）：服务不做缓存/状态，每个请求重跑与 CLI 相同的库函数；
`tests/test_s151_svc.py` 对覆盖命令逐字节比对"服务回包 vs CLI stdout"。
任何异常（起不来/管道断/超时）→ `call()` 返回 None → 调用方走原有按次 spawn
路径（行为、报错语义完全不变）。

开关：`UNIFIED_RX_SVC=off` 强制走 spawn（测试/排障用）。
覆盖域：sys / fs（read|stat|list）/ taint（write 走 stdin 语义，不经服务）。
"""
import atexit
import json
import os
import subprocess
import threading
import time

from tools.appaudit import _rs_exe

_RX_SVC_EXE_NAME = "rx-svc.exe"
_lock = threading.Lock()
_proc = None
_exe_mtime = None
_pending_since = None          # 看门狗用：当前调用开始时刻
_CALL_HARD_TIMEOUT_S = 30


def _svc_exe():
    return _rs_exe(_RX_SVC_EXE_NAME)


def enabled():
    """服务默认开；`UNIFIED_RX_SVC=off` 关闭（测试默认 off，见 conftest）。"""
    return os.environ.get("UNIFIED_RX_SVC", "on").lower() != "off"


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _spawn():
    global _proc, _exe_mtime
    exe = _svc_exe()
    if not exe:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _proc = subprocess.Popen(
        [exe, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
        shell=False, creationflags=flags)
    _exe_mtime = _mtime(exe)
    return True


def _ensure_locked():
    """确保服务在跑且与当前 exe 版本一致（exe 重建 → 重启）。"""
    global _proc
    exe = _svc_exe()
    if not exe:
        return False
    if _proc is not None and _proc.poll() is None and _mtime(exe) == _exe_mtime:
        return True
    _shutdown_locked()
    return _spawn()


def _shutdown_locked():
    global _proc
    if _proc is not None:
        try:
            if _proc.stdin:
                _proc.stdin.close()        # EOF → 子进程自退
            _proc.wait(timeout=3)
        except Exception:                  # noqa: BLE001
            try:
                _proc.kill()
            except Exception:              # noqa: BLE001
                pass
        _proc = None


def stop():
    """显式收尾（测试/排障用；正常流程由 atexit 兜底）。"""
    with _lock:
        _shutdown_locked()


def call(domain, argv, timeout=_CALL_HARD_TIMEOUT_S):
    """一次服务调用。返回 (rc, out_str) 或 None（→ 调用方回退按次 spawn）。

    rc 语义与 CLI 一致：0=工具级结果；2=用法/沙盒拒绝（调用方转 ValueError）。
    """
    if not enabled():
        return None
    global _pending_since, _proc
    with _lock:
        try:
            if not _ensure_locked():
                return None
            req = json.dumps({"domain": domain, "argv": list(argv)},
                             ensure_ascii=False)
            _pending_since = time.monotonic()
            _proc.stdin.write(req + "\n")
            _proc.stdin.flush()
            line = _proc.stdout.readline()
            if not line:
                _proc = None               # 管道断（子进程死了）→ 下次重起
                return None
            rep = json.loads(line)
            rc = int(rep.get("rc", 2))
            out = rep.get("out", "")
            if not isinstance(out, str):
                return None
            return rc, out
        except Exception:                  # noqa: BLE001  # —— 任何异常都回退
            _proc = None
            return None
        finally:
            _pending_since = None


def json_call(domain, argv):
    """便捷封装：返回 (rc, dict) 或 None（服务不可用 → 调用方回退）。

    —— 与各薄壳的 CLI 路径同包络：rc==2 表示"用法/沙盒拒绝"，其 error 字段
    与 CLI 输出逐字节一致（由 tests/test_s151_svc.py 锁定）。
    """
    r = call(domain, argv)
    if r is None:
        return None
    rc, out = r
    try:
        return rc, json.loads(out)
    except ValueError:
        return None


def _watchdog():
    """子进程若在某次调用上卡死超时 → 杀掉（让 readline 解除阻塞，走回退路径）。"""
    while True:
        time.sleep(5)
        with _lock:
            if (_proc is not None and _pending_since is not None
                    and time.monotonic() - _pending_since > _CALL_HARD_TIMEOUT_S):
                try:
                    _proc.kill()
                except Exception:          # noqa: BLE001
                    pass


threading.Thread(target=_watchdog, daemon=True).start()
atexit.register(stop)
