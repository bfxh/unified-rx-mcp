"""tools/lsp.py —— S17 真 LSP 客户端域（stdio JSON-RPC 语言服务器驱动）。

S129 拆分（CONSOLIDATION §三 P1）：本文件收敛为**客户端核心**——服务器探测/
会话管理/请求泵/文本编辑应用；两个 @tool 已平移：
- `ide_lsp` 动作分发 → tools/lsp_actions.py（同包，属性访问本模块状态）；
- `ide_impact` + 降级链 → tools/impact.py（LSP references 只是它的一档）。
测试与 ide_edit 直接引用的工具函数（`_SESSIONS`/`_Session`/`_from_utf16_col`/
`_apply_text_edits`/`validate_content`）全部留在本模块——拆分只动归属，不动语义。

取代被 S15 移除的"文本级伪 LSP"（code_complete/ide_references）：语义级
定义跳转 / 引用 / hover / 符号清单 / 诊断 / 重命名预案，走标准
Language Server Protocol——单点接开源最强（engine.py 同款哲学）：

- Rust   → rust-analyzer（本机 ~/.cargo/bin 实测在位）
- Python → pylsp（python-lsp-server + jedi）
- 其它语言如实报 not wired，绝不假装支持

安全与边界（对齐全项目规矩）：
- 只接受 fs 沙盒内的绝对路径（复用 tools/fs._resolve fail-closed 校验）
- rename 走 textDocument/rename 但【只返回预案不落盘】——落盘是宿主的活
- 服务器子进程带空闲 TTL 回收与会话锁；崩溃即降级报错不挂死宿主
"""
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time

_LSP_SERVERS = {
    "rust": {
        "label": "rust-analyzer",
        "cmd": lambda: (os.environ.get("UNIFIED_RX_LSP_CMD_RUST") or "rust-analyzer").split(),
    },
    "python": {
        "label": "pylsp",
        "cmd": lambda: ([c for c in (os.environ.get("UNIFIED_RX_LSP_CMD_PYTHON") or "").split()]
                        or [sys.executable, "-m", "pylsp"]),
    },
}
def _module_available(name):
    """`python -m <name>` 形态的模块可用性（S99：pylsp 未装时 exe=解释器本身
    会让 which/存在性检查假阳性——detected 必须验到模块层）。"""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _detect_exe(spec):
    """服务器可执行探测：命中返回可执行路径，否则 None。"""
    from shutil import which
    cmd = spec["cmd"]() or [""]
    exe = cmd[0]
    found = which(exe) or (os.path.exists(exe) and exe) or None
    if not found:
        return None
    if len(cmd) >= 3 and cmd[1] == "-m" and not _module_available(cmd[2]):
        return None
    return found


_IDLE_TTL_S = 600          # 空闲回收
_INIT_TIMEOUT = 60         # 首次 initialize/index 上限
_REQ_TIMEOUT = 45

_HEADER_RE = re.compile(rb"Content-Length:\s*(\d+)\r\n", re.IGNORECASE)
_MAX_FRAME_BYTES = 64 * 1024 * 1024   # S62：入站帧上限（服务器异常不撑爆内存）


def _as_uri(path):
    from pathlib import Path
    return Path(path).as_uri()


def _uri_path(uri):
    from urllib.parse import unquote, urlparse
    p = urlparse(uri).path
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    return unquote(p).replace("/", "\\")


def _to_utf16_col(line_text, col_chars):
    """用户给 0-based 字符列 → LSP 默认 UTF-16 code unit 列（中文字符陷阱）。"""
    return len((line_text[:col_chars] or "").encode("utf-16-le")) // 2


def _sanitize(item):
    """Location/Hover 等 → {file,line..end} 最小单元输出。"""
    if not isinstance(item, dict):
        return item
    rng = item.get("range") or item.get("selectionRange") or {}
    out = {}
    if "uri" in item:
        out["file"] = _uri_path(item["uri"])
    s = rng.get("start") or {}
    e = rng.get("end") or {}
    out["line"] = s.get("line")
    out["col"] = s.get("character")
    if e.get("line") is not None or e.get("character") is not None:
        out["end_line"], out["end_col"] = e.get("line"), e.get("character")
    # hover.contents
    cont = item.get("contents")
    if isinstance(cont, dict):
        out["hover"] = str(cont.get("value", ""))[:400]
    elif isinstance(cont, list):
        parts = []
        for c in cont[:3]:
            parts.append(c.get("value", "") if isinstance(c, dict) else str(c))
        out["hover"] = "\n".join(parts)[:400]
    elif isinstance(cont, str):
        out["hover"] = cont[:400]
    if "name" in item:
        out["name"] = item["name"]
    if "kind" in item:
        out["kind"] = item["kind"]
    return out


class _Session:
    """一个语言服务器进程（per language+root），串行请求。"""

    def __init__(self, lang, cmd, root):
        self.lang = lang
        self.root = root
        self.cmd = cmd
        self.proc = None
        self.next_id = itertools.count(1)
        self.lock = threading.Lock()
        self.opened = set()
        self.diagnostics = {}
        self._open_mtimes = {}      # uri -> latest items
        self.last_used = time.time()

    def start(self):
        # 常驻句柄（SIM115 豁免）：stderr 日志随 LSP 进程生命周期，会话回收时关闭
        # （`_err_log.close()`，S18 的 fd 泄漏修复）——with 反而会在 start() 返回时关掉它。
        err_log = open(os.path.join(os.environ.get("TEMP", "."), f"uRX_lsp_{self.lang}.log"),  # noqa: SIM115
                       "ab")
        self._err_log = err_log
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err_log, cwd=self.root,
            env={**os.environ, "PYTHONUTF8": "1"})
        # 常驻读取线程：stdout 阻塞读永不设防（publish 推送类消息没有请求伴随），
        # 队列化后 _read_msg 才能拥有真实的超时语义。
        import queue
        import threading as _th
        self._q = queue.Queue()
        self._alive = True
        t = _th.Thread(target=self._reader, daemon=True)
        t.start()
        resp = self._request_raw("initialize", {
            "processId": os.getpid(),
            "rootUri": _as_uri(self.root),
            "capabilities": {"textDocument": {
                "hover": {"contentFormat": ["plaintext"]},
                "publishDiagnostics": {}}}},
            timeout=_INIT_TIMEOUT)
        if resp is None:
            self.stop()
            raise RuntimeError(f"{self.lang} server initialize 超时")
        self._notify("initialized", {})

    def _reader(self):
        """后台线程：LSP 帧 → 队列。EOF/损坏即退出并压入哨兵。"""
        rd = self.proc.stdout
        try:
            while self._alive:
                header = b""
                while b"\r\n\r\n" not in header:
                    ch = rd.read(1)
                    if not ch:
                        self._q.put(None)
                        return
                    header += ch
                    if len(header) > 4096:
                        raise ConnectionError("bad header")
                n = None
                for hl in header.split(b"\r\n"):
                    if hl.lower().startswith(b"content-length:"):
                        n = int(hl.split(b":")[1].strip())
                if n is None:
                    raise ConnectionError("missing Content-Length")
                if n > _MAX_FRAME_BYTES:
                    raise ConnectionError(f"frame too large: {n}")
                body = b""
                while len(body) < n:
                    chunk = rd.read(n - len(body))
                    if not chunk:
                        raise ConnectionError("server closed mid-body")
                    body += chunk
                self._q.put(json.loads(body.decode("utf-8")))
        except Exception:
            self._q.put(None)

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        self._alive = False
        try:
            if self.alive():
                try:
                    self._request_raw("shutdown", {}, timeout=3)
                    self._notify("exit", {})
                except Exception:
                    pass
                self.proc.kill()
        finally:
            try:
                if self.proc and self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            self.proc = None
            try:
                if getattr(self, "_err_log", None):
                    self._err_log.close()          # S18：会话回收必须带走日志句柄（fd 泄漏修复）
                    self._err_log = None
            except Exception:
                pass

    def _send_frame(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        self.proc.stdin.flush()

    def _read_msg(self, timeout_deadline):
        """从队列取下一条消息；真·超时（deadline 由调用方决定）。"""
        import queue as _queue
        remaining = max(0.05, timeout_deadline - time.monotonic())
        try:
            msg = self._q.get(timeout=remaining)
        except _queue.Empty:
            return None                                     # 超时
        if msg is None:                                     # reader 线程哨兵
            raise ConnectionError("server closed")
        return msg

    def _notify(self, method, params):
        self._send_frame({"jsonrpc": "2.0", "method": method, "params": params})

    def _dispatch(self, msg):
        """下行消息统一归属：服务器请求必答；诊断入缓冲；其余通知忽略。"""
        m = msg.get("method")
        if m and msg.get("id") is not None:
            ans = []
            if m == "workspace/configuration":
                ans = [{}] * len(msg.get("params", {}).get("items") or [1])
            self._send_frame({"jsonrpc": "2.0", "id": msg["id"], "result": ans})
        elif m == "textDocument/publishDiagnostics":
            diags = msg.get("params") or {}
            self.diagnostics[diags.get("uri")] = diags.get("diagnostics") or []

    def _request_raw(self, method, params, timeout=_REQ_TIMEOUT):
        rid = next(self.next_id)
        deadline = time.monotonic() + timeout
        self._send_frame({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self._read_msg(deadline)
            if msg is None:
                return None                      # 超时
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result")
            self._dispatch(msg)

    def ensure_open(self, path):
        if path in self.opened:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(2_000_000)
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": _as_uri(path), "languageId": self.lang,
                             "version": 1, "text": text}})
        self.opened.add(path)
        self._open_mtimes[path] = os.stat(path).st_mtime_ns

    def notify_change(self, path):
        """S50：文件落盘后推 didChange（全文同步）——服务端内容不再陈旧，
        诊断增量推送的前提。版本号单调递增。"""
        if path not in self.opened:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(2_000_000)
        self._version = getattr(self, "_version", 1) + 1
        self._notify("textDocument/didChange", {
            "textDocument": {"uri": _as_uri(path),
                             "version": self._version},
            "contentChanges": [{"text": text}]})

    def refresh_if_stale(self, path):
        """mtime 变了 → 推 didChange + 短泵收新诊断。返回是否推送。"""
        cur = os.stat(path).st_mtime_ns
        if cur == self._open_mtimes.get(path):
            return False
        self.notify_change(path)
        self.pump(4.0)
        self._open_mtimes[path] = os.stat(path).st_mtime_ns
        return True

    def pump(self, seconds):
        """无上行请求地接收下行推送（publishDiagnostics 靠推不靠拉）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = self._read_msg(deadline)
            except (ConnectionError, json.JSONDecodeError):
                break
            if msg is None:
                break
            self._dispatch(msg)

    def call(self, method, params, path=None, timeout=_REQ_TIMEOUT):
        with self.lock:
            if not self.alive():
                self.start()
            if path:
                self.ensure_open(path)
            self.last_used = time.time()
            return self._request_raw(method, params, timeout=timeout)


_SESSIONS: dict[tuple, tuple] = {}
_MGR_LOCK = threading.Lock()


def _get_session(lang, root):
    key = (lang, root)
    entry = _SESSIONS.get(key)
    now = time.time()
    if entry and entry[0].alive() and now - entry[0].last_used < _IDLE_TTL_S:
        return entry[0]
    if entry:
        entry[0].stop()
        del _SESSIONS[key]
    with _MGR_LOCK:
        spec = _LSP_SERVERS.get(lang)
        if not spec:
            raise LookupError(f"语言 {lang} 未接线；可探测: {list(_LSP_SERVERS)}")
        cmd = spec["cmd"]()
        sess = _Session(lang, cmd, root)
        sess.start()
        _SESSIONS[key] = (sess,)
    return sess


def reap_idle():
    now = time.time()
    for k, (sess,) in list(_SESSIONS.items()):
        if now - sess.last_used > _IDLE_TTL_S or not sess.alive():
            sess.stop()
            _SESSIONS.pop(k, None)


def _resolve_in_sandbox(path):
    from .fs import _resolve as fs_resolve
    try:
        return fs_resolve(path)                     # ValueError=越界/垃圾路径
    except ValueError as e:
        raise PermissionError(str(e))


_LANG_BY_EXT = {".rs": "rust", ".py": "python"}


def _session_root(fp):
    """会话根：向上找 .git（项目语义根）；找不到退回文件目录。

    S60 修复：此前 root=文件所在目录 → src/ 与 tests/ 各起一个语言服务器，
    跨文件 definition/references/impact 天然失明（会话分裂，结果残缺）。"""
    cur = os.path.dirname(fp)
    for _ in range(15):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(fp)


def _locate(lang, fp, line, col):
    """(session, uri, {line,character})——含 utf-16 列换算与 didOpen。"""
    sess = _get_session(lang, _session_root(fp))
    with open(fp, encoding="utf-8", errors="replace") as f:
        lines_txt = f.readlines()
    txt = lines_txt[line] if 0 <= line < len(lines_txt) else ""
    pos = {"line": line, "character": _to_utf16_col(txt.rstrip("\n"), max(col, 0))}
    uri = _as_uri(fp)
    return sess, uri, pos


_TRANSIENT_ERR = ("content modified", "file not found", "timed out")


def _call_ready(sess, method, params, waits=(1.0, 2.0, 3.0, 5.0, 8.0)):
    """冷启动期服务器按规范合法返 null 或抛瞬时错（VF3 实测 ra 首响 ~17s；
    未就绪时 references 会直接报 'file not found'）——两者都归入退避重试。
    非瞬时错误立即上浮，不吞真失败。

    首个尝试**不预睡**（S172 实测：热会话原先也要白付第一档 1.0s——补全/定义等
    全部 LSP 动作的耗时地板 1010ms 里 1000ms 是这觉）：服务器已就绪即零等待直返；
    冷启动由退避档兜底，sleep 预算不变（1+2+3+5+8=19s，只分摊到重试之间）。"""
    for w in (0.0, *waits):
        if w:
            time.sleep(w)
        try:
            r = sess.call(method, params, timeout=_REQ_TIMEOUT + int(sum(waits)))
        except RuntimeError as e:
            if any(t in str(e).lower() for t in _TRANSIENT_ERR):
                continue
            raise
        if r not in (None, [], {}):
            return r
    return None


def validate_content(path, text, pump_s=4.0):
    """R1 写前验证：把【未落盘】的内容经 didChange 推给 LSP，泵回诊断。

    返回 {"engine", "errors", "total", "diagnostics"}；LSP 不可用 →
    {"error": ...}（调用方决定放行与否）。发现 error 时自动把磁盘内容
    推回去，不留"验证态"内容污染后续会话诊断。"""
    try:
        real = _resolve_in_sandbox(path)
    except PermissionError as e:
        return {"error": str(e)}
    lang = _LANG_BY_EXT.get(os.path.splitext(real)[1].lower())
    if not lang:
        return {"error": f"validate 不支持扩展名 {os.path.splitext(real)[1]}"}
    try:
        sess = _get_session(lang, _session_root(real))
        sess.ensure_open(real)
        sess._version = getattr(sess, "_version", 1) + 1
        sess._notify("textDocument/didChange", {
            "textDocument": {"uri": _as_uri(real), "version": sess._version},
            "contentChanges": [{"text": text[:2_000_000]}]})
        sess.pump(pump_s)
        items = sess.diagnostics.get(_as_uri(real), [])
        errors = [d for d in items if d.get("severity") == 1]
        if errors:
            sess.notify_change(real)          # 还原磁盘内容，防验证态残留
        ds = [{"severity": {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
                  d.get("severity"), str(d.get("severity"))),
               "line": d.get("range", {}).get("start", {}).get("line"),
               "message": (d.get("message") or "")[:200],
               "source": d.get("source")} for d in errors[:10]]
        return {"engine": f"{lang}-lsp", "errors": len(errors),
                "total": len(items), "diagnostics": ds}
    except (LookupError, FileNotFoundError, RuntimeError, ConnectionError) as e:
        return {"error": f"LSP 不可用: {e}"}
    except Exception as e:                                  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _from_utf16_col(line_text, units):
    """LSP UTF-16 code unit 列 → Python 字符列（_to_utf16_col 的逆）。"""
    if units is None or units <= 0:
        return 0
    chars = 0
    used = 0
    for ch in line_text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > units:
            break
        used += w
        chars += 1
    return chars


def _pos_offset(lines, line, col_units):
    """(line, utf16col) → src 内字符偏移（越界钳到边界，不静默丢编辑）。"""
    if line < 0:
        return 0
    if line >= len(lines):
        return sum(len(l) + 1 for l in lines)
    return (sum(len(l) + 1 for l in lines[:line])
            + _from_utf16_col(lines[line], col_units))


def _apply_text_edits(src, edits):
    """LSP TextEdit 列表 → (新文本, 应用数)。同文档非重叠编辑，按起点倒序拼接。

    行尾保留：按 \\n 切行时 \\r 归前行内容，偏移不受影响；整体写回原样保留。"""
    if not edits:
        return src, 0
    lines = src.split("\n")
    todo = []
    for ed in edits:
        rng = ed.get("range") or {}
        s = rng.get("start") or {}
        e = rng.get("end") or {}
        a = _pos_offset(lines, int(s.get("line") or 0), s.get("character") or 0)
        b = _pos_offset(lines, int(e.get("line") or 0), e.get("character") or 0)
        todo.append((a, b, ed.get("newText") or ""))
    todo.sort(key=lambda t: (t[0], t[1]), reverse=True)
    out = src
    applied = 0
    for a, b, txt in todo:
        a = max(0, min(a, len(out)))
        b = max(a, min(b, len(out)))
        out = out[:a] + txt + out[b:]
        applied += 1
    return out, applied
