# SPDX-License-Identifier: MIT
# RX-IDE Lite 宿主：本地 HTTP 服务（17310）+ 可选 pywebview 窗口。
# 仅使用 Python 标准库；pywebview 为可选依赖（函数内导入）。
"""rxide.host —— RX-IDE Lite 宿主服务。

职责：
1. ThreadingHTTPServer 提供前端静态资源与全部 /api/* 接口（端口 17310）。
2. /preview/* 反向代理到 settings.preview_target，并向 HTML 注入错误捕获脚本。
3. start() 决定纯 Web 模式（--web）或 pywebview 桌面窗口模式。

后端能力全部委托给 rxide 包内模块（settings/ai/commands/diff/termlog），
本文件只做协议转换，不含业务逻辑。
"""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rxide import settings, ai, commands, diff, termlog, agentfeed

HOST = "127.0.0.1"
PORT = 17310
# 前端静态资源根目录：项目根 dist/（React+Vite 构建产物；rxide/web 旧三件套保留在磁盘但不再被服务）
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")
# Monaco 本地资产别名：/vendor/monaco/* → rxide/web/vendor/monaco/*（不复制 13MB 进 dist）
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "vendor", "monaco")
# fs 接口单文件大小上限（2MB）
FS_MAX_BYTES = 2 * 1024 * 1024
# 预览代理单次响应读取上限（16MB，防恶意/失控源拖死线程）
MAX_PREVIEW_BYTES = 16 * 1024 * 1024

# CORS 同源白名单：仅放行本机 17310 页面与空 Origin（curl 等非浏览器客户端）
_ALLOWED_ORIGINS = {"http://127.0.0.1:17310", "http://localhost:17310"}

# 注入到预览页 <head> 之后的错误捕获脚本（window.__rxErrors 环形 50 条）
PREVIEW_INJECT = (
    "<script>(function(){var B=[];window.__rxErrors=B;"
    "function push(e){B.unshift(typeof e===\"string\"?e:(e&&(e.message||e.reason&&e.reason.message)||String(e)));"
    "if(B.length>50)B.pop();}"
    "window.addEventListener(\"error\",function(ev){push(ev.message+\" @\"+(ev.filename||\"\")+\":\"+(ev.lineno||\"\"))});"
    "window.addEventListener(\"unhandledrejection\",function(ev){push(ev.reason)});"
    "var c=console.error;console.error=function(){push([].slice.call(arguments).map(String).join(\" \"));"
    "c.apply(console,arguments);};})();</script>"
)

# 注入到预览页的请求重写补丁：仪表盘内部用绝对路径（如 /api/overview）轮询，
# 直接打到 17310 会 404。此处补丁 iframe 文档内的 fetch/XHR，把以 "/" 开头的
# 绝对 URL 重写为 "/preview" + url（同源经本代理转发回 preview_target）。
# 只作用于注入脚本所在的 iframe 文档，不影响宿主页面；相对路径与协议相对 URL 不动。
PREVIEW_REWRITE = (
    "<script>(function(){function rw(u){"
    "return typeof u===\"string\"&&u.charAt(0)===\"/\"&&u.indexOf(\"//\")!==0?\"/preview\"+u:u;}"
    "var of=window.fetch;"
    "if(of){window.fetch=function(i,o){try{"
    "if(typeof i===\"string\"){i=rw(i);}"
    "else if(i&&i.url){var n=rw(i.url);if(n!==i.url){i=new Request(n,i);}}"
    "}catch(e){}return of.call(this,i,o);};}"
    "var xo=XMLHttpRequest.prototype.open;"
    "XMLHttpRequest.prototype.open=function(){try{arguments[1]=rw(String(arguments[1]));}catch(e){}"
    "return xo.apply(this,arguments);};})();</script>"
)
PREVIEW_INJECT = PREVIEW_INJECT + PREVIEW_REWRITE

# 静态资源 MIME 表
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

# 各语言的 AI 系统提示（简洁专业，说明语言与文件路径）
_LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JSX",
    ".tsx": "TSX", ".html": "HTML", ".css": "CSS", ".json": "JSON",
    ".rs": "Rust", ".go": "Go", ".java": "Java", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".sh": "Shell",
    ".sql": "SQL", ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML",
    ".toml": "TOML", ".vue": "Vue", ".lua": "Lua",
}


def _lang_of(path):
    """按扩展名猜语言，未知返回纯文本。"""
    ext = os.path.splitext(path or "")[1].lower()
    return _LANG_BY_EXT.get(ext, "纯文本")


def _build_messages(kind, body, file_path, ctx_text):
    """按命令类别组装 messages（system 简洁专业，说明语言/文件路径）。"""
    lang = _lang_of(file_path)
    head = "你是资深编程助手，回答简洁专业。目标语言：%s；文件：%s。" % (lang, file_path or "未命名")
    if kind == "explain":
        task = "请解释下面代码（重点是所选函数/片段）的作用与关键点，用简短文字回答，不要输出代码块。"
    elif kind == "fix":
        task = "请修复下面代码中的问题，只返回修正后的完整代码块（```包裹），不要任何解释文字。"
    else:  # edit
        task = "请按需求修改下面代码，只返回修改后的完整代码块（```包裹），不要任何解释文字。"
    return [
        {"role": "system", "content": head + task},
        {"role": "user", "content": "需求：\n%s\n\n当前代码上下文：\n%s" % (body, ctx_text)},
    ]


def _try_apply_edit(file_text, reply, selection, cursor_line):
    """若回复含代码块则应用编辑，返回 (new_text, diff)；否则返回 None。"""
    code = commands.parse_llm_edit(reply)
    if code is None:
        return None
    new_text, _s, _e = diff.apply_edit(file_text, code, selection=selection, cursor_line=cursor_line)
    return new_text, diff.line_diff(file_text, new_text)


class Handler(BaseHTTPRequestHandler):
    """单一路由器：按方法 + 路径分发。"""

    protocol_version = "HTTP/1.1"
    server_version = "RXIDELite/1.0"
    timeout = 30  # 防谎报 Content-Length / 慢速连接挂死线程

    # ---------- 基础工具 ----------
    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _origin_ok(self):
        """防 CSRF drive-by：仅放行同源 Origin 与空 Origin（非浏览器客户端）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin = origin.rstrip("/")
        return origin in _ALLOWED_ORIGINS

    def _send(self, status, body_bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, status, msg):
        self._json({"ok": False, "error": msg}, status)

    def _read_body(self):
        """读取 JSON 请求体，失败返回 {}。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(min(n, 32 * 1024 * 1024))
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # 任何异常都不允许打崩服务
            self._err(500, "内部错误: %s" % e)

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._err(500, "内部错误: %s" % e)

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path == "/":
            path = "/index.html"
        # API 类 GET 同样校验 Origin（/preview/ 与静态资源不受影响）
        if path.startswith("/api/") and not self._origin_ok():
            return self._err(403, "Origin 不被允许")
        if path == "/api/settings":
            return self._json({"ok": True, "data": settings.masked(settings.load())})
        if path == "/api/logtail":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                cursor = int((qs.get("cursor") or ["0"])[0])
            except ValueError:
                cursor = 0
            out = dict(termlog.log_tail(cursor=cursor))
            out["ok"] = True
            return self._json(out)
        # IDE 增强（任务 #15）：只读查询端点——侧栏目录/搜索/git/智能体跟踪
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/api/fs/list":
            out = agentfeed.fs_list((qs.get("path") or [""])[0])
            return self._json(out, 200 if out.get("ok") else 400)
        if path == "/api/search":
            out = agentfeed.search((qs.get("query") or [""])[0],
                                   (qs.get("root") or [""])[0])
            return self._json(out, 200 if out.get("ok") else 400)
        if path == "/api/git/status":
            out = agentfeed.git_status((qs.get("root") or [""])[0])
            return self._json(out, 200 if out.get("ok") else 400)
        if path == "/api/agent/feed":
            try:
                cursor = int((qs.get("cursor") or ["0"])[0])
            except ValueError:
                cursor = 0
            return self._json(agentfeed.agent_feed(cursor))
        if path == "/api/agent/status":
            return self._json(agentfeed.agent_status())
        if path.startswith("/preview/") or path == "/preview":
            return self._proxy_preview(path[len("/preview"):] or "/", parsed.query)
        if path.startswith("/vendor/monaco/"):
            return self._static_vendor(path[len("/vendor/monaco/"):])
        # 其余一律按静态资源处理；无扩展名未知路径回退 index.html（SPA fallback）
        return self._static(path)

    def _route_post(self):
        # 全部 POST 先过 Origin 校验（防跨站驱动本地接口）
        if not self._origin_ok():
            return self._err(403, "Origin 不被允许")
        path = urllib.parse.urlparse(self.path).path
        body = self._read_body()
        if path == "/api/settings":
            settings.save(body if isinstance(body, dict) else {})
            return self._json({"ok": True})
        if path == "/api/context":
            return self._api_context(body)
        if path == "/api/command":
            return self._api_command(body)
        if path == "/api/ai/stream":
            return self._api_stream(body)
        if path == "/api/fs/open":
            return self._api_fs_open(body)
        if path == "/api/fs/save":
            return self._api_fs_save(body)
        return self._err(404, "未知接口: %s" % path)

    # ---------- 业务接口 ----------
    def _api_context(self, body):
        file_text = body.get("file_text") or ""
        cursor_line = int(body.get("cursor_line") or 1)
        selection = body.get("selection")
        full = bool(body.get("full"))
        out = dict(commands.build_context(file_text, cursor_line, selection=selection, full=full))
        out["ok"] = True
        return self._json(out)

    def _api_command(self, body):
        out = self._command_prepare(body)
        if "error" in out:
            return self._err(out["status"], out["error"])
        if out.get("direct"):
            return self._json(out["direct"])
        messages = out["messages"]
        res = ai.chat(messages, cfg=None, stream=False)
        if not res.get("ok"):
            return self._json({"ok": False, "kind": out["kind"], "error": res.get("error", "AI 请求失败")})
        return self._json(self._command_finish(out, res.get("content") or ""))

    def _command_prepare(self, body):
        """命令分发前置：解析/term 直达/组装 messages。
        返回 {"error","status"} 或 {"direct":{...}} 或 {kind,cmd_body,file_text,
        selection,cursor_line,messages}。force=True 强制 full（跳过上下文截断）。"""
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "空命令", "status": 400}
        file_path = body.get("file_path") or ""
        file_text = body.get("file_text") or ""
        cursor_line = int(body.get("cursor_line") or 1)
        selection = body.get("selection")
        force = bool(body.get("force"))
        try:
            parsed = commands.parse(text)
        except Exception as e:
            return {"error": "命令解析失败: %s" % e, "status": 500}
        kind, cmd_body = parsed.get("kind", "edit"), parsed.get("body", text)
        # 终端命令：交给 termlog，直达结果
        if kind == "term":
            res = dict(termlog.run_command(cmd_body))
            res.setdefault("ok", True)
            res["kind"] = "term"
            return {"direct": res}
        # explain / fix / edit：组装上下文 → messages
        full = force or ("// @full" in cmd_body)  # force 强制全量上下文
        try:
            ctx = commands.build_context(file_text, cursor_line, selection=selection, full=full)
        except Exception as e:
            return {"error": "上下文构建失败: %s" % e, "status": 500}
        messages = _build_messages(kind, cmd_body, file_path, ctx.get("context_text", ""))
        return {"kind": kind, "cmd_body": cmd_body, "file_text": file_text,
                "selection": selection, "cursor_line": cursor_line, "messages": messages}

    def _command_finish(self, prep, reply):
        """AI 回复后处理，返回与 /api/command 完全一致的完整结果。"""
        kind = prep["kind"]
        if kind in ("fix", "edit"):
            applied = _try_apply_edit(prep["file_text"], reply, prep["selection"], prep["cursor_line"])
            if applied is not None:
                new_text, d = applied
                return {"ok": True, "kind": "edit", "reply": reply,
                        "new_text": new_text, "diff": d}
        return {"ok": True, "kind": kind, "reply": reply}

    def _api_stream(self, body):
        """SSE 流式命令：接受与 /api/command 相同的 body，复用命令分发。

        事件协议：{"type":"token","text"} 逐 token 推送；
        {"type":"done","result":{与 /api/command 一致的完整结果}}；
        {"type":"error","error"}。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def push(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            prep = self._command_prepare(body)
            if "error" in prep:
                return push({"type": "error", "error": prep["error"]})
            if "direct" in prep:  # term 命令无需流式，单帧 done
                return push({"type": "done", "result": prep["direct"]})
            content = None
            for ev in ai.chat(prep["messages"], cfg=None, stream=True):
                t = ev.get("type")
                if t == "token":
                    push({"type": "token", "text": ev.get("text", "")})
                elif t == "done":
                    content = ev.get("content") or ""
                    break
                elif t == "error":
                    return push({"type": "error", "error": ev.get("error", "AI 请求失败")})
            if content is None:
                return push({"type": "error", "error": "流式响应未正常结束"})
            push({"type": "done", "result": self._command_finish(prep, content)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                push({"type": "error", "error": str(e)})
            except Exception:
                pass

    def _api_fs_open(self, body):
        path = (body.get("path") or "").strip()
        if not path:
            return self._err(400, "缺少 path")
        ap = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(ap):
            return self._err(404, "文件不存在或不是普通文件")
        if os.path.getsize(ap) > FS_MAX_BYTES:
            return self._err(413, "文件超过 2MB，拒绝读取")
        try:
            with open(ap, "r", encoding="utf-8", newline="") as f:
                text = f.read()  # newline="" 保持原 CRLF，防打开再保存翻转行尾
        except UnicodeDecodeError:
            return self._err(415, "不是 UTF-8 文本文件")
        except OSError as e:
            return self._err(500, "读取失败: %s" % e)
        return self._json({"ok": True, "text": text})

    def _api_fs_save(self, body):
        path = (body.get("path") or "").strip()
        text = body.get("text")
        if not path or text is None:
            return self._err(400, "缺少 path/text")
        if not isinstance(text, str):
            return self._err(400, "text 必须是字符串")
        ap = os.path.abspath(os.path.expanduser(path))
        # 路径安全：拒绝目录；保存位置必须是已存在的上级目录内
        if os.path.isdir(ap):
            return self._err(400, "目标是目录")
        parent = os.path.dirname(ap)
        if not os.path.isdir(parent):
            return self._err(400, "上级目录不存在")
        try:
            with open(ap, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as e:
            return self._err(500, "保存失败: %s" % e)
        return self._json({"ok": True})

    # ---------- 静态资源 ----------
    def _serve_file(self, full):
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def _static(self, url_path):
        rel = url_path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        # 防目录穿越
        if not (full == WEB_DIR or full.startswith(WEB_DIR + os.sep)):
            return self._err(403, "拒绝访问")
        if os.path.isfile(full):
            return self._serve_file(full)
        # SPA fallback：无扩展名路径（前端路由如 /editor）回退 index.html；静态资产缺失仍 404
        last = rel.replace("\\", "/").rsplit("/", 1)[-1]
        index = os.path.join(WEB_DIR, "index.html")
        if "." not in last and os.path.isfile(index):
            return self._serve_file(index)
        return self._err(404, "未找到: %s" % url_path)

    def _static_vendor(self, rest):
        """/vendor/monaco/* 别名 → rxide/web/vendor/monaco/*（Monaco 本地资产）。"""
        rel = rest.lstrip("/")
        full = os.path.normpath(os.path.join(VENDOR_DIR, rel))
        if not (full == VENDOR_DIR or full.startswith(VENDOR_DIR + os.sep)):
            return self._err(403, "拒绝访问")
        if not os.path.isfile(full):
            return self._err(404, "未找到: /vendor/monaco/%s" % rel)
        return self._serve_file(full)

    # ---------- 预览反向代理 ----------
    def _proxy_preview(self, rest, query):
        cfg = settings.load()
        target = (cfg.get("preview_target") or "").rstrip("/")
        if not target:
            return self._err(502, "未配置 preview_target")
        # 仅允许 http/https（防 file:// 等 scheme 的 SSRF 读本机文件）
        scheme = urllib.parse.urlparse(target).scheme.lower()
        if scheme not in ("http", "https"):
            return self._err(502, "preview_target 仅支持 http/https 协议")
        url = target + (rest if rest.startswith("/") else "/" + rest)
        if query:
            url += "?" + query
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "RX-IDE-Lite/1.0")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(MAX_PREVIEW_BYTES + 1)
                if len(data) > MAX_PREVIEW_BYTES:
                    data = data[:MAX_PREVIEW_BYTES]  # 超限截断
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        except urllib.error.HTTPError as e:
            return self._err(502, "预览源返回 %s" % e.code)
        except Exception as e:
            return self._err(502, "预览源不可达: %s" % e)
        # 仅对 HTML 注入错误捕获脚本（紧跟 <head> 之后；无 head 则插最前）
        if "html" in ctype.lower():
            try:
                html = data.decode("utf-8", errors="replace")
                low = html.lower()
                i = low.find("<head>")
                if i >= 0:
                    html = html[: i + 6] + PREVIEW_INJECT + html[i + 6:]
                else:
                    html = PREVIEW_INJECT + html
                data = html.encode("utf-8")
            except Exception:
                pass
        self._send(200, data, ctype if "charset" in ctype.lower() else ctype)


class Bridge:
    """pywebview JS 桥：文件对话框。"""

    def open_file_dialog(self):
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            if not win:
                return None
            r = win.create_file_dialog(dialog_type=webview.OPEN_DIALOG, allow_multiple=False)
            if r:
                return r[0]
            return None
        except Exception:
            return None

    def save_file_dialog(self, default_path=""):
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            if not win:
                return None
            kw = {"dialog_type": webview.SAVE_DIALOG}
            if default_path:
                kw["save_filename"] = default_path
            r = win.create_file_dialog(**kw)
            if isinstance(r, (list, tuple)):
                return r[0] if r else None
            return r
        except Exception:
            return None


_server = None


def _start_http():
    """启动 HTTP 后台线程，返回 server 实例。"""
    global _server
    if _server is not None:
        return _server
    _server = ThreadingHTTPServer((HOST, PORT), Handler)
    _server.daemon_threads = True
    t = threading.Thread(target=_server.serve_forever, name="rxide-http", daemon=True)
    t.start()
    return _server


def start(web_only=False):
    """启动宿主。web_only=True 仅 HTTP（阻塞）；否则拉起 pywebview 窗口。"""
    _start_http()
    url = "http://%s:%d/" % (HOST, PORT)
    if web_only:
        print("[RX-IDE Lite] HTTP 服务已启动: %s （Ctrl+C 退出）" % url)
        try:
            threading.Event().wait()  # 阻塞主线程
        except KeyboardInterrupt:
            print("\n[RX-IDE Lite] 已退出")
        return

    # 桌面窗口模式：pywebview 为可选依赖
    try:
        import webview
    except ImportError:
        print("[RX-IDE Lite] 未安装 pywebview，回退纯 Web 模式: %s" % url)
        print("  安装命令: python -m pip install pywebview")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return
    webview.create_window(
        "RX-IDE Lite", url,
        width=1280, height=800,
        background_color="#1a1a1a",
        js_api=Bridge(),
    )
    webview.start(gui=None)


if __name__ == "__main__":
    start(web_only=True)
