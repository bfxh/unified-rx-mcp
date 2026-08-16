#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx 桌面 IDE（tkinter 零依赖）——IDE 界面 + 各种功能窗口。

主窗口（IDE 布局）：
  菜单栏 / 左：项目文件树 / 中：编辑器（行号+语法高亮+保存）/
  右：功能面板（工具调用/扫描/遥测/热榜/日志）/ 底：状态栏

功能窗口（菜单或面板触发）：
  工具调用器（server._call 全链路）/ 扫描面板 / 遥测 / 仪表盘（Canvas
  条形图）/ 扫描日志 / 关于

数据源：~/.unified-rx/（stats/scan-log/telemetry）+ server 注册表——
复用 dashboard.py 的纯读取函数（零重复）。

用法：python ide_ui.py
"""
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dashboard import _read_jsonl, _read_stats, _scanlog, _telemetry, _tools  # noqa: E402

DATA_DIR = os.path.expanduser("~/.unified-rx")
START_TS = time.time()

# ── 配色（深色 IDE 风格）──
C = dict(bg="#0d1117", panel="#161b22", line="#30363d", fg="#e6edf3",
         dim="#8b949e", acc="#58a6ff", ok="#3fb950", warn="#d29922",
         err="#f85149", sel="#1f6feb", editor="#0d1117", gutter="#161b22",
         keyword="#ff7b72", string="#a5d6ff", comment="#8b949e",
         number="#79c0ff", fn="#d2a8ff")

# 编辑器语法高亮（关键词集——按语言）
_KW = {
    "python": r"\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|lambda|pass|break|continue|raise|yield|global|nonlocal|and|or|not|in|is|None|True|False|self|async|await)\b",
    "rust": r"\b(fn|let|mut|pub|struct|enum|impl|trait|match|if|else|for|while|loop|return|use|mod|self|Self|async|await|move|ref|dyn|where|const|static|unsafe|type|true|false|Some|None|Ok|Err|Result|Option)\b",
    "json": r"\b(true|false|null)\b",
}
_STR_RE = r"\x22(?:[^\x5c\x22]|\x5c.)*\x22|\x27(?:[^\x5c\x27]|\x5c.)*\x27"
_CMT_RE = {"python": r"#[^\n]*", "rust": r"(//[^\n]*|/\*.*?\*/)"}
_NUM_RE = r"\b\d[\d_.]*\b"


def _lang_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py",):
        return "python"
    if ext in (".rs",):
        return "rust"
    if ext in (".json",):
        return "json"
    return ""


def _fmt_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=1)
    except Exception:
        return str(obj)

# ─────────────────────────────────────────────────────────────
# 编辑器组件：行号 + Text + 语法高亮 + 保存
# ─────────────────────────────────────────────────────────────
class CodeEditor(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.path = None
        self.lang = ""
        self._gutter = tk.Canvas(self, width=46, bg=C["gutter"],
                                 highlightthickness=0)
        self._gutter.pack(side="left", fill="y")
        self._text = tk.Text(self, bg=C["editor"], fg=C["fg"],
                             insertbackground=C["fg"], wrap="none",
                             font=("Consolas", 11), undo=True,
                             relief="flat", padx=6, pady=4)
        self._text.pack(side="left", fill="both", expand=True)
        self._sb = ttk.Scrollbar(self, command=self._text.yview)
        self._sb.pack(side="right", fill="y")
        self._text.configure(yscrollcommand=self._sync_scroll)
        self._text.bind("<KeyRelease>", lambda e: self._highlight())
        self._text.bind("<Control-s>", lambda e: self.save())
        self._text.bind("<Control-o>", lambda e: self.open_dialog())
        self._text.bind("<Configure>", lambda e: self._draw_gutter())

    def _sync_scroll(self, *a):
        self._sb.set(*a)
        self._draw_gutter()

    def _draw_gutter(self):
        self._gutter.delete("all")
        first = self._text.index("@0,0")
        last = self._text.index("@0,1000000")
        line = int(first.split(".")[0])
        end = int(last.split(".")[0])
        y = 0
        for ln in range(line, end + 1):
            idx = self._text.index(f"{ln}.0")
            y = self._text.dlineinfo(idx)
            if y is None:
                continue
            self._gutter.create_text(40, y[1] + 2, anchor="ne",
                                     text=str(ln), fill=C["dim"],
                                     font=("Consolas", 10))

    def open_file(self, path):
        try:
            size = os.path.getsize(path)
            if size > 1_000_000:
                messagebox.showwarning("文件过大", f"{os.path.basename(path)} 超过 1MB，已跳过")
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror("打开失败", str(e))
            return
        self.path = path
        self.lang = _lang_of(path)
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)
        self._text.edit_reset()
        self._highlight()
        self._draw_gutter()

    def open_dialog(self):
        p = filedialog.askopenfilename(
            title="打开文件", initialdir=os.path.expanduser("~"))
        if p:
            self.open_file(p)

    def save(self):
        if not self.path:
            p = filedialog.asksaveasfilename(title="保存为", defaultextension=".txt")
            if not p:
                return
            self.path = p
        try:
            with open(self.path, "w", encoding="utf-8", newline="") as f:
                f.write(self._text.get("1.0", "end-1c"))
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.master.status("已保存 " + os.path.basename(self.path))

    def _clear_tags(self):
        for t in ("kw", "str", "cmt", "num", "fnc"):
            self._text.tag_remove(t, "1.0", "end")

    def _highlight(self):
        if not self.lang:
            return
        self._clear_tags()
        text = self._text.get("1.0", "end-1c")
        n = len(text)
        if n > 500_000:  # 大文件跳过高亮（防卡）
            return
        base = "1.0"

        def tag(pat, tag_name, group=0, flags=0):
            try:
                for m in re.finditer(pat, text, flags):
                    s = self._text.index(f"{base}+{m.start(group)}c")
                    e = self._text.index(f"{base}+{m.end(group)}c")
                    self._text.tag_add(tag_name, s, e)
            except (re.error, tk.TclError):
                pass

        # 先字符串/注释（高优先级底色），再关键词
        tag(_STR_RE, "str", flags=re.S)
        cmt = _CMT_RE.get(self.lang)
        if cmt:
            tag(cmt, "cmt", flags=re.S)
        tag(_KW.get(self.lang, ""), "kw", flags=re.I)
        tag(_NUM_RE, "num")
        # 函数调用 fnc( 高亮（python/rust）
        tag(r"([a-zA-Z_]\w*)(?=\s*\()", "fnc")
        for t, color in (("kw", C["keyword"]), ("str", C["string"]),
                         ("cmt", C["comment"]), ("num", C["number"]),
                         ("fnc", C["fn"])):
            self._text.tag_configure(t, foreground=color)

# ─────────────────────────────────────────────────────────────
# 文件树（懒加载：展开时读子目录）
# ─────────────────────────────────────────────────────────────
_SKIP_DIRS = {".git", "target", "__pycache__", "node_modules", ".idea",
              ".vscode", "vendor", "dist", "build", ".unified-rx-index"}


class FileTree(tk.Frame):
    def __init__(self, master, on_open, **kw):
        super().__init__(master, **kw)
        self.on_open = on_open
        self.root_path = None
        self._tree = ttk.Treeview(self, show="tree", selectmode="browse")
        self._tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self, command=self._tree.yview)
        sb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<<TreeviewOpen>>", self._on_open_node)
        self._tree.bind("<Double-1>", self._on_double)
        self._tree.bind("<Return>", self._on_double)

    def load_root(self, path):
        self.root_path = path
        self._tree.delete(*self._tree.get_children())
        node = self._tree.insert("", "end", text=os.path.basename(path) or path,
                                 open=True)
        self._load_children(node, path)

    def _load_children(self, node, path):
        try:
            entries = sorted(os.scandir(path), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in _SKIP_DIRS:
                continue
            if e.is_dir():
                child = self._tree.insert(node, "end", text="📁 " + e.name,
                                          open=False)
                # 占位子节点（触发懒加载）
                self._tree.insert(child, "end", text="")
            else:
                self._tree.insert(node, "end", text=e.name,
                                  tags=("file",),
                                  values=(os.path.join(path, e.name),))

    def _on_open_node(self, event):
        node = event.widget.focus()
        kids = self._tree.get_children(node)
        if len(kids) == 1 and self._tree.item(kids[0], "text") == "":
            self._tree.delete(kids[0])
            path = self._node_path(node)
            if path and os.path.isdir(path):
                self._load_children(node, path)

    def _node_path(self, node):
        parts = []
        while node:
            parts.append(self._tree.item(node, "text").replace("📁 ", ""))
            node = self._tree.parent(node)
        if not parts:
            return None
        parts.reverse()
        head = parts[0] if parts else ""
        if self.root_path and os.path.basename(self.root_path) == head:
            return os.path.join(self.root_path, *parts[1:])
        return os.path.join(*parts) if len(parts) > 1 else head

    def _on_double(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        if vals:
            self.on_open(vals[0])

# ─────────────────────────────────────────────────────────────
# 功能面板（右栏 Notebook）：工具调用 / 扫描 / 热榜 / 日志
# ─────────────────────────────────────────────────────────────
class ToolPanel(tk.Frame):
    """工具调用器：下拉选工具 → JSON 参数 → 运行 → 结果。"""

    def __init__(self, master, server, **kw):
        super().__init__(master, **kw)
        self.server = server
        self._tools = sorted(server._TOOLS.keys())
        row = tk.Frame(self, bg=C["panel"])
        row.pack(fill="x", padx=6, pady=4)
        tk.Label(row, text="工具", bg=C["panel"], fg=C["dim"]).pack(side="left")
        self._combo = ttk.Combobox(row, values=self._tools, width=28)
        self._combo.pack(side="left", padx=6)
        self._combo.bind("<<ComboboxSelected>>", lambda e: self._load_args())
        self._run_btn = tk.Button(row, text="▶ 运行", command=self.run,
                                  bg=C["sel"], fg="white", relief="flat")
        self._run_btn.pack(side="left", padx=6)
        tk.Label(self, text="参数（JSON，可空）", bg=C["panel"],
                 fg=C["dim"]).pack(anchor="w", padx=6)
        self._args = tk.Text(self, height=5, bg=C["editor"], fg=C["fg"],
                             insertbackground=C["fg"], font=("Consolas", 10),
                             relief="flat")
        self._args.pack(fill="x", padx=6)
        tk.Label(self, text="结果", bg=C["panel"], fg=C["dim"]).pack(anchor="w", padx=6)
        self._out = tk.Text(self, bg=C["editor"], fg=C["fg"], wrap="none",
                            font=("Consolas", 10), relief="flat")
        self._out.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _load_args(self):
        name = self._combo.get()
        fn, sc, _ = self.server._TOOLS.get(name, (None, None, None))
        if sc:
            self._args.delete("1.0", "end")
            props = sc.get("properties", {})
            req = sc.get("required", [])
            self._args.insert("1.0", _fmt_json({r: "" for r in req}))

    def run(self):
        name = self._combo.get()
        if not name:
            return
        raw = self._args.get("1.0", "end-1c").strip()
        args = {}
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                self._out.delete("1.0", "end")
                self._out.insert("1.0", f"参数 JSON 非法: {e}")
                return
        self._out.delete("1.0", "end")
        self._out.insert("1.0", "运行中…")

        def work():
            try:
                r = self.server._call(name, args)
                text = r[0].text if isinstance(r, list) else str(r)
                out = _fmt_json(json.loads(text)) if text.startswith("{") else text
            except Exception as e:  # noqa: BLE001
                out = f"Error: {type(e).__name__}: {e}"
            self.after(0, lambda: self._set_out(out))

        threading.Thread(target=work, daemon=True).start()

    def _set_out(self, text):
        self._out.delete("1.0", "end")
        self._out.insert("1.0", text)


class ScanPanel(tk.Frame):
    """扫描：bug_scan/std_check/vuln_scan/ui_check → 结果表。"""

    def __init__(self, master, server, **kw):
        super().__init__(master, **kw)
        self.server = server
        row = tk.Frame(self, bg=C["panel"])
        row.pack(fill="x", padx=6, pady=4)
        tk.Label(row, text="路径", bg=C["panel"], fg=C["dim"]).pack(side="left")
        self._path = tk.Entry(row, bg=C["editor"], fg=C["fg"],
                              insertbackground=C["fg"])
        self._path.pack(side="left", fill="x", expand=True, padx=6)
        self._path.insert(0, HERE)
        self._kind = ttk.Combobox(row, width=12, values=(
            "bug_scan", "std_check", "vuln_scan", "ui_check"))
        self._kind.set("bug_scan")
        self._kind.pack(side="left")
        tk.Button(row, text="▶ 扫描", command=self.run,
                  bg=C["sel"], fg="white", relief="flat").pack(side="left", padx=6)
        cols = ("severity", "file", "line", "msg")
        self._tree = ttk.Treeview(self, columns=cols, show="headings",
                                  height=12)
        for c, w in zip(cols, (70, 180, 50, 420)):
            self._tree.heading(c, text=c)
            self._tree.column(c, width=w)
        self._tree.pack(fill="both", expand=True, padx=6, pady=4)

    def run(self):
        path = self._path.get().strip()
        kind = self._kind.get()
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._tree.insert("", "end", values=("…", kind, "", "扫描中"))

        def work():
            try:
                r = self.server._call(kind, {"path": path})
                text = r[0].text if isinstance(r, list) else str(r)
                d = json.loads(text) if text.startswith("{") else {}
                issues = d.get("issues", []) if isinstance(d, dict) else []
                rows = [(i.get("severity", "?"), (i.get("file") or "").split("\\")[-1],
                         i.get("line", ""), (i.get("msg") or "")[:120])
                        for i in issues]
            except Exception as e:  # noqa: BLE001
                rows = [("ERR", kind, "", str(e)[:120])]
            self.after(0, lambda: self._set_rows(rows))

        threading.Thread(target=work, daemon=True).start()

    def _set_rows(self, rows):
        for i in self._tree.get_children():
            self._tree.delete(i)
        for r in rows:
            tag = ""
            if r[0] in ("error", "ERR"):
                tag = "err"
            elif r[0] == "warning":
                tag = "warn"
            self._tree.insert("", "end", values=r, tags=(tag,))
        self._tree.tag_configure("err", foreground=C["err"])
        self._tree.tag_configure("warn", foreground=C["warn"])


class StatsPanel(tk.Frame):
    """热榜（Canvas 条形图）+ 累计统计。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._canvas = tk.Canvas(self, bg=C["panel"], highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self.after(2000, self.refresh)

    def refresh(self):
        st = _read_stats()
        top = sorted(st.get("by_tool", {}).items(),
                     key=lambda kv: -kv[1])[:10]
        self._canvas.delete("all")
        cw = self._canvas.winfo_width() or 360
        ch = self._canvas.winfo_height() or 300
        maxv = max((v for _, v in top), default=1)
        self._canvas.create_text(cw // 2, 14, text=f"TOP10 · 累计 {st.get('total', 0):,} 次",
                                 fill=C["fg"], font=("Segoe UI", 11, "bold"))
        y = 34
        for name, v in top:
            w = max(10, int((cw - 180) * v / maxv))
            self._canvas.create_text(140, y + 8, text=name, anchor="e",
                                     fill=C["dim"], font=("Consolas", 9))
            self._canvas.create_rectangle(150, y, 150 + w, y + 16,
                                          fill=C["acc"], outline="")
            self._canvas.create_text(160 + w, y + 8, text=f"{v:,}", anchor="w",
                                     fill=C["fg"], font=("Consolas", 9))
            y += 22
        self.after(3000, self.refresh)


class LogPanel(tk.Frame):
    """scan-log 最近记录。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        cols = ("ts", "tool", "ok", "summary")
        self._tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, w in zip(cols, (110, 90, 40, 460)):
            self._tree.heading(c, text=c)
            self._tree.column(c, width=w)
        self._tree.pack(fill="both", expand=True)
        self.refresh()
        self.after(5000, self._auto)

    def _auto(self):
        self.refresh()
        self.after(5000, self._auto)

    def refresh(self):
        recs = _scanlog(30)
        for i in self._tree.get_children():
            self._tree.delete(i)
        for r in recs:
            self._tree.insert("", "end", values=(
                str(r.get("ts", ""))[:19], r.get("tool", ""),
                "OK" if r.get("ok") else "FAIL", r.get("summary", "")))

# ─────────────────────────────────────────────────────────────
# 独立功能窗口（Toplevel）：遥测 / 关于
# ─────────────────────────────────────────────────────────────
class TelemetryWin(tk.Toplevel):
    """遥测窗口：慢工具 TOP / 错误率 / daemon 心跳。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("遥测")
        self.geometry("520x420")
        self.configure(bg=C["bg"])
        self._txt = tk.Text(self, bg=C["editor"], fg=C["fg"], wrap="word",
                            font=("Consolas", 10), relief="flat")
        self._txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.refresh()
        self.after(3000, self._auto)

    def _auto(self):
        if self.winfo_exists():
            self.refresh()
            self.after(3000, self._auto)

    def refresh(self):
        tel = _telemetry(300)
        ov = None
        try:
            import dashboard
            ov = dashboard._overview()
        except Exception:
            pass
        lines = [f"遥测样本（最近 300）: {tel['samples']}",
                 f"错误率: {tel['err_rate'] * 100:.1f}%（{tel['err_count']} 次）", ""]
        lines.append("最慢工具 TOP8:")
        for s in tel["slowest"]:
            lines.append(f"  {s['tool']:<24} {s['ms']:>9.1f}ms  {s['status']}")
        if ov and ov.get("heartbeats"):
            lines.append("")
            lines.append("daemon 心跳:")
            now = time.time()
            for k, ts in ov["heartbeats"].items():
                age = now - ts
                lines.append(f"  {k:<20} {age:.0f}s 前" + (" ✓" if age < 300 else " ✗"))
        self._txt.delete("1.0", "end")
        self._txt.insert("1.0", "\n".join(lines))


class AboutWin(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("关于")
        self.geometry("380x300")
        self.configure(bg=C["bg"])
        t = _tools()
        st = _read_stats()
        info = (f"unified-rx 桌面 IDE\n\n"
                f"工具: {t.get('total', 0)}（核心 {t.get('core_count', 0)} + 扩展 {t.get('ext_count', 0)}）\n"
                f"累计调用: {st.get('total', 0):,}\n\n"
                f"数据目录: {DATA_DIR}\n"
                f"仓库: {HERE}\n\n"
                f"tkinter {tk.TkVersion} · 零第三方依赖\n"
                f"2026-08-16 · bfxh")
        tk.Label(self, text=info, bg=C["bg"], fg=C["fg"], justify="left",
                 font=("Segoe UI", 11)).pack(padx=20, pady=20, anchor="w")

# ─────────────────────────────────────────────────────────────
# 主窗口：菜单 + 文件树 + 编辑器 + 功能面板 + 状态栏
# ─────────────────────────────────────────────────────────────
class IdeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("unified-rx 桌面 IDE")
        self.geometry("1280x800")
        self.configure(bg=C["bg"])
        try:
            import server as S
            self.server = S
        except Exception as e:  # noqa: BLE001
            self.server = None
            messagebox.showwarning("server 不可用", str(e))
        self._build_layout()
        self._build_menu()
        self.status("就绪")
        self.after(3000, self._tick)

    # ── 布局 ──
    def _build_layout(self):
        main = tk.PanedWindow(self, orient="horizontal", bg=C["bg"],
                              sashwidth=4, sashrelief="flat")
        main.pack(fill="both", expand=True)
        # 左：文件树
        left = tk.Frame(main, bg=C["panel"])
        tk.Label(left, text="📁 项目文件", bg=C["panel"], fg=C["dim"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=4)
        self._tree = FileTree(left, on_open=self._open_file)
        self._tree.pack(fill="both", expand=True, padx=4, pady=4)
        tk.Button(left, text="… 选择项目目录", command=self._choose_root,
                  bg=C["panel"], fg=C["acc"], relief="flat",
                  activebackground=C["line"]).pack(fill="x", padx=4, pady=(0, 4))
        main.add(left, width=240, minsize=160)
        # 中：编辑器
        mid = tk.Frame(main, bg=C["panel"])
        self._editor = CodeEditor(mid)
        self._editor.pack(fill="both", expand=True, padx=2, pady=2)
        main.add(mid, width=640, minsize=320)
        # 右：功能面板
        right = tk.Frame(main, bg=C["panel"])
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        if self.server:
            nb.add(ToolPanel(nb, self.server), text="🔧 工具")
            nb.add(ScanPanel(nb, self.server), text="🩺 扫描")
        nb.add(StatsPanel(nb), text="📊 热榜")
        nb.add(LogPanel(nb), text="📜 日志")
        main.add(right, width=400, minsize=280)
        # 底：状态栏
        bar = tk.Frame(self, bg=C["panel"], height=26)
        bar.pack(fill="x", side="bottom")
        self._status_lbl = tk.Label(bar, text="", bg=C["panel"], fg=C["dim"],
                                    anchor="w", font=("Segoe UI", 9))
        self._status_lbl.pack(side="left", padx=8)
        self._tick_lbl = tk.Label(bar, text="", bg=C["panel"], fg=C["ok"],
                                  font=("Consolas", 9))
        self._tick_lbl.pack(side="right", padx=8)

    # ── 菜单 ──
    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="打开文件…  Ctrl+O", command=self._editor.open_dialog)
        fm.add_command(label="保存  Ctrl+S", command=self._editor.save)
        fm.add_separator()
        fm.add_command(label="退出", command=self.destroy)
        m.add_cascade(label="文件", menu=fm)
        tm = tk.Menu(m, tearoff=0)
        tm.add_command(label="工具调用器", command=lambda: self._panel_tab(0))
        tm.add_command(label="扫描", command=lambda: self._panel_tab(1))
        tm.add_command(label="遥测窗口", command=self._open_telemetry)
        tm.add_command(label="仪表盘网页 (:17300)", command=self._open_web)
        m.add_cascade(label="工具", menu=tm)
        vm = tk.Menu(m, tearoff=0)
        vm.add_command(label="切换左右面板", command=self._toggle_tree)
        m.add_cascade(label="视图", menu=vm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="关于", command=self._open_about)
        m.add_cascade(label="帮助", menu=hm)
        self.config(menu=m)

    # ── 交互 ──
    def _choose_root(self):
        p = filedialog.askdirectory(initialdir=os.path.expanduser("~"))
        if p:
            self._tree.load_root(p)
            self.status("项目: " + p)

    def _open_file(self, path):
        if os.path.isfile(path):
            self._editor.open_file(path)
            self.status(os.path.basename(path))

    def _panel_tab(self, idx):
        nb = self.winfo_children()[0].winfo_children()[1].winfo_children()[0]
        try:
            nb.select(idx)
        except Exception:
            pass

    def _open_telemetry(self):
        TelemetryWin(self)

    def _open_about(self):
        AboutWin(self)

    def _open_web(self):
        import webbrowser
        webbrowser.open("http://127.0.0.1:17300")

    def _toggle_tree(self):
        pass  # 面板宽度由 PanedWindow sash 拖动，保留菜单项

    def status(self, msg):
        self._status_lbl.configure(text="  " + msg)

    # ── 状态栏定时刷新（3s）──
    def _tick(self):
        t = _tools()
        st = _read_stats()
        ov = None
        try:
            import dashboard
            ov = dashboard._overview()
        except Exception:
            pass
        fresh = ov["data_latest_age_s"] if ov else -1
        color = C["ok"] if 0 <= fresh < 600 else C["warn"]
        self._tick_lbl.configure(
            text=f"工具 {t.get('total', 0)} · 调用 {st.get('total', 0):,} · 数据 {fresh}s 前",
            fg=color)
        self.after(3000, self._tick)


def main() -> int:
    app = IdeApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
