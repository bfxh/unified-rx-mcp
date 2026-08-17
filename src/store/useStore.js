import { create } from 'zustand';

/**
 * RX-IDE Lite 全局状态。
 * 任务 #16：Edge 式主标签（mainTabs，多编辑器实例会话快照 sessions）、
 * 右侧八面板侧栏（sidePanel/sideCollapsed/sideWidth）、底部面板
 * （bottomOpen/bottomTab/bottomHeight）、智能体 feed 缓存与受控状态。
 * 字号与主题为本地偏好（localStorage 即时应用）；preview_target 由后端持久化。
 */
const BLANK_SESSION = { openFiles: [], filePath: '', editorCode: '' };

const KIND_LABEL = {
  editor: '编辑器', browser: '浏览器', terminal: '终端',
  live: '实时调用', tools: '工具热榜',
};

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function restoreSession(patch, sessions, tabId) {
  const sess = sessions[tabId] || BLANK_SESSION;
  patch.openFiles = sess.openFiles;
  patch.filePath = sess.filePath;
  patch.editorCode = sess.editorCode;
  patch.cursorLine = 1;
  patch.cursorCol = 1;
  patch.selection = null;
  patch.selectedLines = 0;
  patch.selectedText = '';
}

const useStore = create((set, get) => ({
  // ---------- Edge 式主标签 ----------
  mainTabs: [
    { id: 'm-editor', kind: 'editor', label: '编辑器' },
    { id: 'm-browser', kind: 'browser', label: '浏览器' },
    { id: 'm-terminal', kind: 'terminal', label: '终端' },
    { id: 'm-live', kind: 'live', label: '实时调用' },
    { id: 'm-tools', kind: 'tools', label: '工具热榜' },
  ],
  activeMainTabId: 'm-editor',
  sessions: {}, // editorTabId → {openFiles, filePath, editorCode}

  activateMainTab: (id) =>
    set((s) => {
      if (id === s.activeMainTabId) return s;
      const from = s.mainTabs.find((t) => t.id === s.activeMainTabId);
      const to = s.mainTabs.find((t) => t.id === id);
      if (!to) return s;
      const patch = { activeMainTabId: id };
      if (from && from.kind === 'editor') {
        patch.sessions = {
          ...s.sessions,
          [from.id]: { openFiles: s.openFiles, filePath: s.filePath, editorCode: s.editorCode },
        };
      }
      if (to.kind === 'editor') restoreSession(patch, patch.sessions || s.sessions, to.id);
      return patch;
    }),

  /** 兼容旧调用（CommandBar 等）：按 kind 激活；不存在则新建该 kind 标签。 */
  setActiveTab: (kind) => {
    const s = get();
    const exist = s.mainTabs.find((t) => t.kind === kind);
    if (exist) return s.activateMainTab(exist.id);
    const id = kind + '-' + Date.now();
    set((st) => ({
      mainTabs: [...st.mainTabs, { id, kind, label: KIND_LABEL[kind] || kind }],
    }));
    get().activateMainTab(id);
  },

  closeMainTab: (id) =>
    set((s) => {
      if (s.mainTabs.length <= 1) return s; // 至少保留一个
      const idx = s.mainTabs.findIndex((t) => t.id === id);
      if (idx < 0) return s;
      const tabs = s.mainTabs.filter((t) => t.id !== id);
      const sessions = { ...s.sessions };
      delete sessions[id];
      const patch = { mainTabs: tabs, sessions };
      if (s.activeMainTabId === id) {
        const next = tabs[Math.min(idx, tabs.length - 1)];
        patch.activeMainTabId = next.id;
        if (next.kind === 'editor') restoreSession(patch, sessions, next.id);
      }
      return patch;
    }),

  /** "+" 新开编辑器标签：新空白编辑页入 FileTabs 体系。 */
  openNewEditorTab: () =>
    set((s) => {
      const n = s.mainTabs.filter((t) => t.kind === 'editor').length + 1;
      const id = 'ed-' + Date.now();
      const from = s.mainTabs.find((t) => t.id === s.activeMainTabId);
      const sessions = { ...s.sessions };
      if (from && from.kind === 'editor') {
        sessions[from.id] = { openFiles: s.openFiles, filePath: s.filePath, editorCode: s.editorCode };
      }
      return {
        mainTabs: [...s.mainTabs, { id, kind: 'editor', label: '编辑器 ' + n }],
        sessions,
        activeMainTabId: id,
        ...BLANK_SESSION,
        cursorLine: 1,
        cursorCol: 1,
        selection: null,
        selectedLines: 0,
        selectedText: '',
      };
    }),

  // ---------- Editor ----------
  editorCode: '',
  setEditorCode: (code) => set({ editorCode: code }),
  filePath: '',
  setFilePath: (p) => set({ filePath: p }),
  cursorLine: 1,
  setCursorLine: (n) => set({ cursorLine: n }),
  cursorCol: 1,
  setCursorCol: (n) => set({ cursorCol: n }),

  // 已打开文件标签页：[{ path, code, dirty }]；filePath 为当前激活
  openFiles: [],
  openFileEntry: (path, code) =>
    set((s) => {
      const list = s.openFiles.map((f) =>
        f.path === s.filePath ? { ...f, code: s.editorCode } : f
      );
      const exist = list.find((f) => f.path === path);
      const next = exist ? list : [...list, { path, code, dirty: false }];
      const entry = next.find((f) => f.path === path);
      return {
        openFiles: next,
        filePath: path,
        editorCode: entry.code,
        cursorLine: 1,
        cursorCol: 1,
        selection: null,
        selectedLines: 0,
        selectedText: '',
      };
    }),
  switchFileEntry: (path) =>
    set((s) => {
      if (path === s.filePath) return s;
      const list = s.openFiles.map((f) =>
        f.path === s.filePath ? { ...f, code: s.editorCode } : f
      );
      const entry = list.find((f) => f.path === path);
      if (!entry) return s;
      return {
        openFiles: list,
        filePath: path,
        editorCode: entry.code,
        cursorLine: 1,
        cursorCol: 1,
        selection: null,
        selectedLines: 0,
        selectedText: '',
      };
    }),
  editActiveEntry: (code) =>
    set((s) => ({
      editorCode: code,
      openFiles: s.openFiles.map((f) =>
        f.path === s.filePath ? { ...f, code, dirty: true } : f
      ),
    })),
  saveActiveEntry: () =>
    set((s) => ({
      openFiles: s.openFiles.map((f) =>
        f.path === s.filePath ? { ...f, code: s.editorCode, dirty: false } : f
      ),
    })),
  closeFileEntry: (path) =>
    set((s) => {
      const idx = s.openFiles.findIndex((f) => f.path === path);
      if (idx < 0) return s;
      const list = s.openFiles.filter((f) => f.path !== path);
      if (path !== s.filePath) return { openFiles: list };
      const next = list[Math.min(idx, Math.max(0, list.length - 1))];
      return {
        openFiles: list,
        filePath: next ? next.path : '',
        editorCode: next ? next.code : '',
        cursorLine: 1,
        cursorCol: 1,
        selection: null,
        selectedLines: 0,
        selectedText: '',
      };
    }),

  // Selection（非空选区：start/end 为 1-based 行号，供后端 selection 参数）
  selection: null,
  selectedLines: 0,
  selectedText: '',
  setSelection: (sel, lines, text) => set({ selection: sel, selectedLines: lines, selectedText: text }),

  // AI context（由后端 /api/context 计算，防抖 + 请求序号在组件侧处理）
  contextLines: 0,
  setContextLines: (lines) => set({ contextLines: lines }),

  // Command bar
  commandInput: '',
  setCommandInput: (input) => set({ commandInput: input }),
  commandBarVisible: true,
  toggleCommandBar: () => set((s) => ({ commandBarVisible: !s.commandBarVisible })),
  setCommandBarVisible: (v) => set({ commandBarVisible: v }),

  // AI 状态：loading + 流式进度文案
  aiLoading: false,
  setAiLoading: (v) => set({ aiLoading: v }),
  aiStatus: '',
  setAiStatus: (t) => set({ aiStatus: t }),

  // 后端 diff 结果（apply edit 后）：{ name, diff:{added,removed,stats,previews} }
  diffState: null,
  setDiffState: (d) => set({ diffState: d }),
  diffPanelOpen: false,
  setDiffPanelOpen: (v) => set({ diffPanelOpen: v }),

  // Diff preview popup（后端 previews 条目：{file, line, before:[], after:[]}）
  diffPreview: null,
  setDiffPreview: (preview) => set({ diffPreview: preview }),

  // 一次性临时提示条（explain/fix 文本结果、错误）
  toast: null,
  setToast: (t) => set({ toast: t }),

  // Browser
  browserUrl: '/preview/',
  setBrowserUrl: (url) => set({ browserUrl: url }),
  capturedError: '',
  setCapturedError: (err) => set({ capturedError: err }),

  // Terminal（命令结果本地追加 + logtail 轮询增量）
  terminalLogs: [
    { type: 'info', text: '> RX-IDE Lite Terminal — 只读日志视图' },
    { type: 'info', text: '> 命令结果与系统日志显示于此（3s 增量轮询）' },
  ],
  addTerminalLog: (log) =>
    set((s) => ({ terminalLogs: [...s.terminalLogs, log].slice(-5000) })),
  appendTerminalLines: (lines) =>
    set((s) => ({
      terminalLogs: [...s.terminalLogs, ...lines.map((t) => ({ type: 'log', text: t }))].slice(-5000),
    })),

  // ---------- 智能体 feed 缓存 + 受控状态（App 轮询写入，多视图共享） ----------
  feedEvents: [],
  feedCursor: 0,
  ingestFeed: (events, cursor, reset) =>
    set((s) => ({
      feedEvents: reset ? (events || []).slice() : [...s.feedEvents, ...(events || [])].slice(-2000),
      feedCursor: cursor,
    })),
  agentStatus: null,
  setAgentStatus: (st) => set({ agentStatus: st }),

  // ---------- 右侧栏 / 底部面板（尺寸 localStorage 持久化） ----------
  sidePanel: 'explorer',
  setSidePanel: (id) =>
    set((s) => ({ sidePanel: s.sidePanel === id ? null : id, sideCollapsed: false })),
  sideCollapsed: false,
  setSideCollapsed: (v) => set({ sideCollapsed: v }),
  sideWidth: clamp(parseInt(localStorage.getItem('rx_side_w'), 10) || 240, 160, 480),
  setSideWidth: (w) => set({ sideWidth: clamp(w, 160, 480) }),

  bottomOpen: true,
  setBottomOpen: (v) => set({ bottomOpen: v }),
  bottomTab: 'telemetry',
  setBottomTab: (t) => set({ bottomTab: t, bottomOpen: true }),
  bottomHeight: clamp(parseInt(localStorage.getItem('rx_bottom_h'), 10) || 160, 80, 400),
  setBottomHeight: (h) => set({ bottomHeight: clamp(h, 80, 400) }),

  // Settings
  settingsOpen: false,
  setSettingsOpen: (v) => set({ settingsOpen: v }),
  fontSize: parseInt(localStorage.getItem('rx_ide_font_size')) || 13,
  setFontSize: (size) => {
    localStorage.setItem('rx_ide_font_size', String(size));
    set({ fontSize: size });
  },
  theme: localStorage.getItem('rx_ide_theme') || 'dark',
  setTheme: (theme) => {
    localStorage.setItem('rx_ide_theme', theme);
    set({ theme });
  },
}));

export default useStore;
