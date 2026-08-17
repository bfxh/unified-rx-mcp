/* RX-IDE Lite 前端逻辑：三标签 + 全局命令栏 + Monaco + 预览代理。
   无任何第三方依赖；Monaco 通过 AMD loader 加载。 */
"use strict";

// ============ 全局状态 ============
const S = {
  settings: { api_key: "", base_url: "", model: "", font_size: 13, theme: "dark", preview_target: "" },
  filePath: "",
  editor: null,
  monaco: null,
  decorations: [],       // 当前 diff 装饰 id
  viewZones: [],         // 当前 view zone id
  fadeTimer: null,
  busy: false,
  logCursor: 0,
  ctxTimer: null,
  ctxSeq: 0,             // /api/context 请求序号，丢弃乱序的过期响应
  termMenuOpen: false,   // 终端右键菜单打开期间暂停日志追加渲染
  termPending: [],       // 菜单打开期间缓冲的日志行，关闭后补齐
  lastDiff: null,        // 最近一次 diff 结果（含 previews）
};

const $ = (id) => document.getElementById(id);

// ============ 工具 ============
async function api(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json().catch(() => ({ ok: false, error: "响应解析失败(" + r.status + ")" }));
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function baseName(p) {
  if (!p) return "未命名";
  return p.split(/[\\/]/).pop() || p;
}

function langOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  const m = { py: "python", js: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript",
    html: "html", css: "css", json: "json", rs: "rust", go: "go", java: "java", c: "c", h: "c",
    cpp: "cpp", cs: "csharp", rb: "ruby", php: "php", sh: "shell", sql: "sql", md: "markdown",
    yml: "yaml", yaml: "yaml", toml: "ini", vue: "html", lua: "lua", txt: "plaintext" };
  return m[ext] || "plaintext";
}

// ============ 标签切换 ============
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => switchPane(btn.dataset.pane));
});

function switchPane(name) {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.pane === name));
  document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.id === "pane-" + name));
  if (name === "editor") {
    if (S.editor) { S.editor.layout(); S.editor.focus(); }
    updateSelHint(); scheduleContext();
  } else {
    // 离开编辑器标签：清空选区标记与上下文计数，避免失焦后残留
    $("cmd-sel-hint").textContent = "";
    $("cmd-ctx").textContent = "📄 上下文: 0 行";
  }
}

// ============ Monaco ============
function initMonaco() {
  if (typeof require !== "function") {
    $("editor-placeholder").textContent = "Monaco 加载失败：vendor/monaco/vs 缺失";
    return;
  }
  require.config({ paths: { vs: "vendor/monaco/vs" } });
  require(["vs/editor/editor.main"], () => {
    S.monaco = monaco;
    S.editor = monaco.editor.create($("editor-host"), {
      value: "",
      language: "plaintext",
      theme: S.settings.theme === "light" ? "vs" : "vs-dark",
      fontSize: Number(S.settings.font_size) || 13,
      fontFamily: "Consolas, 'Cascadia Mono', monospace",
      fontWeight: "300",
      minimap: { enabled: false },
      automaticLayout: true,
      scrollBeyondLastLine: false,
      renderLineHighlight: "line",
      cursorBlinking: "smooth",
      padding: { top: 10 },
    });
    $("editor-placeholder").classList.add("hidden");
    // 选区严格以 onDidChangeCursorSelection 为准；空选区→清空标记与上下文计数
    S.editor.onDidChangeCursorSelection(() => { updateSelHint(); scheduleContext(); });
    S.editor.onDidChangeCursorPosition(() => scheduleContext());
    S.editor.onDidChangeModelContent(() => scheduleContext());
    updateSelHint(); scheduleContext();
  });
}

function editorText() { return S.editor ? S.editor.getValue() : ""; }

function cursorLine() {
  return S.editor ? S.editor.getPosition().lineNumber : 1;
}

function currentSelection() {
  if (!S.editor) return null;
  const sel = S.editor.getSelection();
  if (!sel || sel.isEmpty()) return null;
  return {
    start: sel.startLineNumber,
    end: sel.endLineNumber,
    text: S.editor.getModel().getValueInRange(sel),
  };
}

// ============ 命令栏：选区提示 + 上下文行数 ============
function updateSelHint() {
  const sel = currentSelection(); // 空选区返回 null
  const el = $("cmd-sel-hint");
  if (sel) {
    // 非空选区即显示（单行选区也显示 [已选 1 行]）
    el.textContent = "[已选 " + (sel.end - sel.start + 1) + " 行]";
  } else {
    // 空选区：清空选区标记与上下文计数（光标移动/内容变化会重新拉取）
    el.textContent = "";
    $("cmd-ctx").textContent = "📄 上下文: 0 行";
  }
}

function scheduleContext() {
  clearTimeout(S.ctxTimer);
  S.ctxTimer = setTimeout(fetchContext, 800); // 节流
}

async function fetchContext() {
  const seq = ++S.ctxSeq;
  try {
    const r = await api("/api/context", {
      file_text: editorText(),
      cursor_line: cursorLine(),
      selection: currentSelection(),
      full: false,
    });
    if (seq !== S.ctxSeq) return; // 已有更新的请求：丢弃过期响应，防止计数跳变
    if (r && r.ok) $("cmd-ctx").textContent = "📄 上下文: " + (r.line_count || 0) + " 行";
  } catch (e) { /* 静默 */ }
}

// ============ 命令发送（AI 类走流式，失败回退非流式） ============
function sendCommand(force) {
  const input = $("cmd-input");
  const text = input.value.trim();
  if (!text) return;
  // force（Ctrl/Cmd+Shift+Enter）绕过 busy 拦截；后端对 force 强制 full 上下文
  if (S.busy && !force) return;
  input.value = "";
  S.busy = true;
  const st = $("cmd-status");
  st.textContent = "思考中";
  st.classList.add("busy");
  hideToast(); hideDiffFloat();

  const body = {
    text: text,
    file_path: S.filePath,
    file_text: editorText(),
    cursor_line: cursorLine(),
    selection: currentSelection(),
    force: !!force,
  };
  const finish = () => {
    S.busy = false;
    st.textContent = "";
    st.classList.remove("busy");
  };
  const fallback = () => api("/api/command", body)
    .then(res => handleCommandResult(text, res))
    .catch(e => showToast("请求失败: " + e, true));

  // term 类（"> " 前缀）无需流式；AI 类先试 /api/ai/stream，失败回退 /api/command
  if (text.startsWith(">")) {
    api("/api/command", body)
      .then(res => handleCommandResult(text, res))
      .catch(e => showToast("请求失败: " + e, true))
      .finally(finish);
    return;
  }
  streamCommand(body, st)
    .then(ok => { if (!ok) return fallback(); })
    .catch(() => fallback())
    .finally(finish);
}

// SSE 流式命令：流式期间只更新命令栏进度，不渲染半截内容；done 后一次性应用
async function streamCommand(body, st) {
  let resp;
  try {
    resp = await fetch("/api/ai/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) { return false; }
  const ct = resp.headers.get("Content-Type") || "";
  if (!resp.ok || !ct.includes("text/event-stream") || !resp.body) return false;
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", chars = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith("data: ")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (ev.type === "token") {
          chars += (ev.text || "").length;
          st.textContent = "流式接收中 " + chars + " 字符"; // 仅进度，不渲染内容
        } else if (ev.type === "done") {
          handleCommandResult(body.text, ev.result || { ok: false, error: "空结果" });
          return true;
        } else if (ev.type === "error") {
          return false; // 回退非流式
        }
      }
    }
  } catch (e) {
    return false; // 流读取失败 → 回退
  }
  return false; // 未收到 done 即结束 → 回退
}

function handleCommandResult(cmdText, res) {
  if (!res || res.ok === false) {
    // term 类未知命令可能带 available 列表
    let msg = (res && res.error) || "命令执行失败";
    if (res && Array.isArray(res.available) && res.available.length) {
      msg += "\n可用命令: " + res.available.join("  ");
    }
    appendTermLine("$ " + cmdText, "cmd");
    appendTermLine(msg, "err");
    showToast(msg, true);
    switchPane("term");
    return;
  }
  if (res.kind === "term") {
    appendTermLine("$ " + cmdText, "cmd");
    if (res.output) appendTermLine(String(res.output).replace(/\n$/, ""));
    if (res.error) appendTermLine(String(res.error), "err");
    if (Array.isArray(res.available) && res.available.length) {
      appendTermLine("可用命令: " + res.available.join("  "), "info");
    }
    if (!res.output && !res.error && !(res.available || []).length) {
      const clone = Object.assign({}, res);
      delete clone.ok; delete clone.kind;
      appendTermLine(JSON.stringify(clone));
    }
    switchPane("term");
    return;
  }
  if (res.kind === "edit" && typeof res.new_text === "string") {
    applyEdit(res);
    return;
  }
  // explain / fix 纯文字结果：一次性临时提示条
  showToast(res.reply || "（无返回内容）");
}

// ============ 编辑应用 + 行内 diff 着色 ============
const DECOR_LIMIT = 500;

function applyEdit(res) {
  if (!S.editor) { showToast("编辑器未就绪", true); return; }
  const d = res.diff || { added: [], removed: [], stats: { add: 0, del: 0 }, previews: [] };
  S.lastDiff = d;
  S.editor.setValue(res.new_text);
  clearDecorations();

  const total = (d.stats && (d.stats.add + d.stats.del)) || 0;
  if (total > DECOR_LIMIT) {
    // 超出上限：折叠提示，不着色
    renderChangesPanel(d, true);
    scheduleContext();
    return;
  }

  const monaco = S.monaco;
  const decos = [];
  (d.added || []).forEach(ln => {
    decos.push({
      range: new monaco.Range(ln, 1, ln, 1),
      options: { isWholeLine: true, className: "rx-add" },
    });
  });
  S.decorations = S.editor.deltaDecorations([], decos);

  // 删除行：view zone 近似渲染
  const removed = d.removed || [];
  if (removed.length) {
    S.editor.changeViewZones(acc => {
      removed.forEach(r => {
        const node = document.createElement("div");
        node.className = "rx-del-zone";
        node.textContent = "- " + String(r.content).replace(/\n/g, "\n- ");
        const lines = String(r.content).split("\n").length;
        S.viewZones.push(acc.addZone({
          afterLineNumber: r.after_line || 0,
          heightInLines: lines,
          domNode: node,
        }));
      });
    });
  }

  // 2 秒后淡化：移除 zone，装饰换极淡标记
  clearTimeout(S.fadeTimer);
  S.fadeTimer = setTimeout(() => {
    if (!S.editor) return;
    S.editor.changeViewZones(acc => S.viewZones.forEach(z => acc.removeZone(z)));
    S.viewZones = [];
    const soft = (d.added || []).map(ln => ({
      range: new monaco.Range(ln, 1, ln, 1),
      options: { isWholeLine: true, className: "rx-soft-add" },
    }));
    S.decorations = S.editor.deltaDecorations(S.decorations, []);
    S.decorations = S.editor.deltaDecorations([], soft);
  }, 2000);

  renderChangesPanel(d, false);
  switchPane("editor");
  scheduleContext();
}

function clearDecorations() {
  clearTimeout(S.fadeTimer);
  if (!S.editor) return;
  if (S.viewZones.length) {
    S.editor.changeViewZones(acc => S.viewZones.forEach(z => acc.removeZone(z)));
    S.viewZones = [];
  }
  S.decorations = S.editor.deltaDecorations(S.decorations, []);
}

// ============ 改动面板 ============
function renderChangesPanel(d, folded) {
  const panel = $("changes-panel");
  const list = $("changes-list");
  list.innerHTML = "";
  const name = baseName(S.filePath);
  const stat = d.stats || { add: 0, del: 0 };
  if (folded) {
    const note = document.createElement("div");
    note.className = "change-item has-note";
    note.textContent = "改动 " + (stat.add + stat.del) + " 行，超出上限已折叠高亮";
    list.appendChild(note);
  }
  const item = document.createElement("div");
  item.className = "change-item";
  item.innerHTML = '<span class="c-name">' + esc(name) + '</span>' +
    '<span class="c-stat"><span class="c-add">+' + stat.add + '</span> ' +
    '<span class="c-del">-' + stat.del + '</span></span>';
  item.addEventListener("click", () => jumpToChange(d));
  list.appendChild(item);
  panel.classList.add("open");           // AI 修改后自动展开
  $("changes-toggle").textContent = "›";
}

$("changes-toggle").addEventListener("click", () => {
  const p = $("changes-panel");
  p.classList.toggle("open");
  $("changes-toggle").textContent = p.classList.contains("open") ? "›" : "‹";
  if (S.editor) setTimeout(() => S.editor.layout(), 240);
});

function jumpToChange(d) {
  const pv = (d.previews && d.previews[0]) || null;
  const line = pv ? pv.line : 1;
  switchPane("editor");
  if (S.editor) {
    S.editor.revealLineInCenter(line);
    S.editor.setPosition({ lineNumber: line, column: 1 });
    S.editor.focus();
  }
  showDiffFloat(d, baseName(S.filePath));
}

// ============ diff 预览浮条（120px，ESC 秒关） ============
function showDiffFloat(d, name) {
  const f = $("diff-float");
  $("diff-float-title").textContent = name + " · 第 " + ((d.previews && d.previews[0] && d.previews[0].line) || 1) + " 行附近";
  const body = $("diff-float-body");
  body.innerHTML = "";
  (d.previews || []).forEach(pv => {
    (pv.before || []).forEach(l => {
      body.innerHTML += '<div class="df-del">- ' + esc(l) + "</div>";
    });
    (pv.after || []).forEach(l => {
      body.innerHTML += '<div class="df-add">+ ' + esc(l) + "</div>";
    });
  });
  if (!body.innerHTML) body.innerHTML = '<div class="df-add">（无预览内容）</div>';
  f.classList.remove("hidden");
}

function hideDiffFloat() { $("diff-float").classList.add("hidden"); }

// ============ 一次性临时提示条（4s 自动淡出，错误类 6s；×/ESC 手动关闭） ============
let toastTimer = null, toastFadeTimer = null;

function showToast(text, isError) {
  const t = $("toast");
  clearTimeout(toastTimer); clearTimeout(toastFadeTimer);
  t.classList.remove("fade");
  $("toast-text").textContent = text;
  t.classList.remove("hidden");
  toastTimer = setTimeout(() => {
    t.classList.add("fade"); // 淡出动画后移除
    toastFadeTimer = setTimeout(() => {
      t.classList.add("hidden");
      t.classList.remove("fade");
    }, 300);
  }, isError ? 6000 : 4000);
}

function hideToast() {
  clearTimeout(toastTimer); clearTimeout(toastFadeTimer);
  const t = $("toast");
  t.classList.add("hidden");
  t.classList.remove("fade");
}
$("toast-close").addEventListener("click", hideToast);
$("toast").addEventListener("click", e => { if (e.target.id === "toast" || e.target.id === "toast-text") hideToast(); });

// ============ 命令栏事件 ============
$("cmd-input").addEventListener("keydown", e => {
  if (e.key === "Enter") {
    e.preventDefault();
    sendCommand(e.ctrlKey || e.metaKey); // Ctrl/Cmd+Shift+Enter 亦走此分支（force）
  }
});

document.addEventListener("keydown", e => {
  const mod = e.ctrlKey || e.metaKey;
  // Ctrl/Cmd+Shift+I 显隐命令栏。
  // 注：该组合键在普通浏览器中与 DevTools 冲突；pywebview 原生窗口下无此问题，
  // 故保留不改（见任务 #6 问题清单第 6 项）。
  if (mod && e.shiftKey && (e.key === "I" || e.key === "i")) {
    e.preventDefault();
    $("cmdbar").classList.toggle("hidden");
    if (S.editor) setTimeout(() => S.editor.layout(), 50);
    return;
  }
  if (e.key === "Escape") {
    hideDiffFloat(); hideToast(); closeSettings(); closeTermMenu();
    return;
  }
  // Ctrl+O 打开 / Ctrl+S 保存
  if (mod && !e.shiftKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); openFile(); }
  if (mod && !e.shiftKey && (e.key === "s" || e.key === "S")) { e.preventDefault(); saveFile(); }
});

// ============ 浏览器标签 ============
function refreshPreviewUrl() {
  $("browser-url").textContent = "/preview/  ←  " + (S.settings.preview_target || "未配置");
}
$("btn-reload").addEventListener("click", () => {
  const f = $("preview-frame");
  f.src = "/preview/?_t=" + Date.now();
});
$("btn-capture-error").addEventListener("click", () => {
  let errors = null;
  try {
    errors = $("preview-frame").contentWindow && $("preview-frame").contentWindow.__rxErrors;
  } catch (e) {
    showToast("无法读取预览页（可能跨源）");
    return;
  }
  if (!errors) { showToast("预览页无错误捕获脚本或尚无报错"); return; }
  if (!errors.length) { showToast("预览页暂无报错"); return; }
  const msg = String(errors[0]);
  $("cmd-input").value = "fix: " + msg;
  $("cmd-input").focus();
  switchPane("editor");
});

// ============ 终端标签 ============
const TERM_MAX = 5000; // 环形缓冲 5000 行

function appendTermLine(text, cls) {
  // 终端右键菜单打开期间暂停渲染（避免 DOM 重排导致点击落空），缓冲后补齐
  if (S.termMenuOpen) {
    if (S.termPending.length < TERM_MAX) S.termPending.push({ text, cls }); // 上限防膨胀
    return;
  }
  const view = $("term-view");
  String(text).split("\n").forEach(line => {
    const div = document.createElement("div");
    div.className = "term-line" + (cls ? " " + cls : "");
    div.textContent = line;
    view.appendChild(div);
  });
  while (view.childElementCount > TERM_MAX) view.removeChild(view.firstChild);
  view.scrollTop = view.scrollHeight;
}

function flushTermPending() {
  if (!S.termPending.length) return;
  const pending = S.termPending;
  S.termPending = [];
  pending.forEach(p => appendTermLine(p.text, p.cls));
}

// 轮询 /api/logtail（3s；在途标志防重入重复追加）
let logtailInFlight = false;
setInterval(async () => {
  if (logtailInFlight) return;
  logtailInFlight = true;
  try {
    const r = await fetch("/api/logtail?cursor=" + S.logCursor);
    const j = await r.json();
    if (j && j.ok) {
      (j.lines || []).forEach(l => appendTermLine(l));
      if (typeof j.cursor === "number") S.logCursor = j.cursor; // 防 0 被 || 吞
    }
  } catch (e) { /* 静默 */ }
  logtailInFlight = false;
}, 3000);

// 终端右键菜单（打开期间暂停日志追加渲染，关闭后恢复并补齐）
const termMenu = $("term-menu");

function closeTermMenu() {
  if (!S.termMenuOpen) { termMenu.classList.add("hidden"); return; }
  S.termMenuOpen = false;
  termMenu.classList.add("hidden");
  flushTermPending();
}

$("term-view").addEventListener("contextmenu", e => {
  e.preventDefault();
  S.termMenuOpen = true;
  termMenu.style.left = e.clientX + "px";
  termMenu.style.top = e.clientY + "px";
  termMenu.classList.remove("hidden");
});
document.addEventListener("click", () => closeTermMenu());
termMenu.addEventListener("click", e => {
  const act = e.target.dataset && e.target.dataset.act;
  if (!act) return;
  const sel = window.getSelection().toString();
  if (!sel) return;
  if (act === "copy") {
    try { navigator.clipboard.writeText(sel); } catch (err) { /* 忽略 */ }
  } else if (act === "to-cmd") {
    $("cmd-input").value = sel.trim();
    $("cmd-input").focus();
  }
});

// ============ 设置 ============
$("btn-settings").addEventListener("click", openSettings);
$("btn-set-cancel").addEventListener("click", closeSettings);
$("settings-mask").addEventListener("click", e => { if (e.target.id === "settings-mask") closeSettings(); });

let maskedKeyOrig = ""; // 打开时的掩码值：未改动则保存时不回传 api_key

async function openSettings() {
  const r = await api("/api/settings");
  if (r && r.ok) S.settings = Object.assign(S.settings, r.data);
  maskedKeyOrig = S.settings.api_key || "";
  $("set-api-key").value = maskedKeyOrig;
  $("set-font-size").value = S.settings.font_size || 13;
  $("set-theme").value = S.settings.theme || "dark";
  $("set-preview").value = S.settings.preview_target || "";
  $("settings-mask").classList.remove("hidden");
}

function closeSettings() { $("settings-mask").classList.add("hidden"); }

$("btn-set-save").addEventListener("click", async () => {
  const patch = {
    font_size: Number($("set-font-size").value) || 13,
    theme: $("set-theme").value,
    preview_target: $("set-preview").value.trim(),
  };
  // API Key 输入框未改动 → 不带 api_key，避免把掩码值写回真实配置
  const keyVal = $("set-api-key").value.trim();
  if (keyVal !== maskedKeyOrig) patch.api_key = keyVal;
  const r = await api("/api/settings", patch);
  if (r && r.ok) {
    Object.assign(S.settings, patch);
    applyTheme();
    refreshPreviewUrl();
    $("preview-frame").src = "/preview/?_t=" + Date.now();
  }
  closeSettings(); // 保存即关
});

function applyTheme() {
  document.body.dataset.theme = S.settings.theme === "light" ? "light" : "dark";
  document.documentElement.style.setProperty("--font-size", (Number(S.settings.font_size) || 13) + "px");
  if (S.editor) {
    S.editor.updateOptions({ fontSize: Number(S.settings.font_size) || 13 });
    S.monaco.editor.setTheme(S.settings.theme === "light" ? "vs" : "vs-dark");
  }
}

// ============ 文件打开 / 保存（pywebview 桥 + 降级 prompt） ============
async function pickOpenPath() {
  try {
    if (window.pywebview && window.pywebview.api) {
      const p = await window.pywebview.api.open_file_dialog();
      if (p) return p;
    }
  } catch (e) { /* 降级 */ }
  return prompt("输入要打开的文件绝对路径:");
}

async function pickSavePath() {
  try {
    if (window.pywebview && window.pywebview.api) {
      const p = await window.pywebview.api.save_file_dialog(S.filePath || "");
      if (p) return p;
    }
  } catch (e) { /* 降级 */ }
  return prompt("输入保存路径:", S.filePath || "");
}

async function openFile() {
  const path = await pickOpenPath();
  if (!path) return;
  const r = await api("/api/fs/open", { path });
  if (!r.ok) { showToast("打开失败: " + r.error, true); return; }
  S.filePath = path;
  $("file-indicator").textContent = path;
  $("file-indicator").title = path + " · Ctrl+O 打开 · Ctrl+S 保存";
  if (S.editor) {
    const model = S.editor.getModel();
    S.monaco.editor.setModelLanguage(model, langOf(path));
    S.editor.setValue(r.text || "");
    clearDecorations();
  }
  scheduleContext();
}

async function saveFile() {
  let path = S.filePath;
  if (!path) path = await pickSavePath();
  if (!path) return;
  const r = await api("/api/fs/save", { path, text: editorText() });
  if (r.ok) {
    S.filePath = path;
    $("file-indicator").textContent = path;
    appendTermLine("已保存: " + path, "info");
  } else {
    showToast("保存失败: " + r.error, true);
  }
}

// ============ 启动 ============
(async function boot() {
  try {
    const r = await api("/api/settings");
    if (r && r.ok) S.settings = Object.assign(S.settings, r.data);
  } catch (e) { /* 使用默认 */ }
  applyTheme();
  refreshPreviewUrl();
  initMonaco();
  appendTermLine("RX-IDE Lite 终端就绪。命令结果与系统日志显示于此。", "info");
  $("cmd-input").focus();
})();
