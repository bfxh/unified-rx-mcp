import { useRef, useCallback, useEffect, useState } from 'react';
import Editor, { loader } from '@monaco-editor/react';
import './EditorPanel.css';
import FileTabs from './FileTabs';
import Breadcrumb from './Breadcrumb';
import useStore from '../store/useStore';
import { fsOpen, fsSave } from '../utils/ai';
import { editorRef as globalEditorRef } from '../utils/editorRef';

// Monaco 本地化：走 host.py 的 /vendor/monaco/vs 静态别名（不依赖 CDN）
loader.config({
  paths: {
    vs: '/vendor/monaco/vs',
  },
});

const DEFAULT_CODE = `// RX-IDE Lite — 极简专业 AI 辅助工具
// 选中代码后直接输入需求，AI 会直接修改编辑器内容
// 命令前缀: > (终端) /explain (解释) /fix (修复)

function fibonacci(n) {
  if (n <= 1) return n;
  return fibonacci(n - 1) + fibonacci(n - 2);
}

function processData(items) {
  // TODO: 优化这段数据处理逻辑
  const results = [];
  for (let i = 0; i < items.length; i++) {
    if (items[i] > 0) {
      results.push(items[i] * 2);
    }
  }
  return results;
}

class DataCache {
  constructor() {
    this.cache = new Map();
  }
  get(key) { return this.cache.get(key); }
  set(key, value) { this.cache.set(key, value); }
}

// 测试入口
const data = [1, -2, 3, -4, 5];
console.log(processData(data));
console.log(fibonacci(10));
`;

function baseName(p) {
  if (!p) return '未命名';
  return p.split(/[\\/]/).pop() || p;
}

/** 扩展名 → Monaco language id（按需加载 basic-languages/*） */
const LANG_ID_BY_EXT = {
  '.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
  '.ts': 'typescript', '.tsx': 'typescript', '.json': 'json', '.html': 'html',
  '.css': 'css', '.md': 'markdown', '.rs': 'rust', '.go': 'go', '.java': 'java',
  '.c': 'c', '.h': 'c', '.cpp': 'cpp', '.cs': 'csharp', '.rb': 'ruby',
  '.php': 'php', '.sh': 'shell', '.sql': 'sql', '.yml': 'yaml', '.yaml': 'yaml',
  '.toml': 'ini', '.lua': 'lua', '.xml': 'xml', '.txt': 'plaintext',
};

function languageOf(path) {
  if (!path) return 'javascript';
  const dot = path.lastIndexOf('.');
  if (dot < 0) return 'plaintext';
  return LANG_ID_BY_EXT[path.slice(dot).toLowerCase()] || 'plaintext';
}

export default function EditorPanel() {
  const editorRef = useRef(null);
  const monacoRef = useRef(null);
  const decorationsRef = useRef([]);
  const zoneIdsRef = useRef([]);
  const fadeTimerRef = useRef(null);

  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);

  const editorCode = useStore((s) => s.editorCode);
  const setEditorCode = useStore((s) => s.setEditorCode);
  const editActiveEntry = useStore((s) => s.editActiveEntry);
  const filePath = useStore((s) => s.filePath);
  const setCursorLine = useStore((s) => s.setCursorLine);
  const setCursorCol = useStore((s) => s.setCursorCol);
  const setSelection = useStore((s) => s.setSelection);
  const diffState = useStore((s) => s.diffState);
  const setDiffPanelOpen = useStore((s) => s.setDiffPanelOpen);
  const fontSize = useStore((s) => s.fontSize);
  const diffPanelOpen = useStore((s) => s.diffPanelOpen);
  const theme = useStore((s) => s.theme);
  const setToast = useStore((s) => s.setToast);
  const agentStatus = useStore((s) => s.agentStatus);

  // 受控指示（任务 #16）：agent active 且 last_write.path === 当前文件 → 编辑器绿框
  const controlled = !!(
    agentStatus && agentStatus.active && agentStatus.last_write
    && agentStatus.last_write.path && filePath
    && agentStatus.last_write.path === filePath
  );

  // 初始默认代码（未打开任何文件时）
  useEffect(() => {
    if (!editorCode) setEditorCode(DEFAULT_CODE);
  }, [editorCode, setEditorCode]);

  const handleEditorMount = useCallback((editor, monaco) => {
    editorRef.current = editor;
    globalEditorRef.current = editor; // 供大纲/搜索/受控 revealLine 使用
    monacoRef.current = monaco;
    setReady(true);

    // 深色主题（VS Code 深色系 + #d4a373 点缀）
    monaco.editor.defineTheme('rx-dark', {
      base: 'vs-dark',
      inherit: true,
      rules: [
        { token: 'comment', foreground: '5a5a5a', fontStyle: 'italic' },
        { token: 'keyword', foreground: 'd4a373' },
        { token: 'string', foreground: '98c379' },
        { token: 'number', foreground: 'd4a373' },
        { token: 'type', foreground: 'd4a373' },
        { token: 'function', foreground: 'dcdcaa' },
      ],
      colors: {
        'editor.background': '#1a1a1a',
        'editor.foreground': '#d4d4d4',
        'editor.lineHighlightBackground': '#252526',
        'editor.selectionBackground': 'rgba(212,163,115,0.2)',
        'editorCursor.foreground': '#d4a373',
        'editorLineNumber.foreground': '#5a5a5a',
        'editorLineNumber.activeForeground': '#888888',
        'editorGutter.background': '#1a1a1a',
        'editor.selectionHighlightBackground': 'rgba(212,163,115,0.08)',
        'editorBracketMatch.background': 'rgba(212,163,115,0.1)',
        'editorBracketMatch.border': 'rgba(212,163,115,0.3)',
      },
    });
    // 浅色主题（VS Code Light 系 + #b07d3f 点缀）
    monaco.editor.defineTheme('rx-light', {
      base: 'vs',
      inherit: true,
      rules: [
        { token: 'comment', foreground: 'a0a0a0', fontStyle: 'italic' },
        { token: 'keyword', foreground: 'b07d3f' },
        { token: 'string', foreground: '4d7c3a' },
        { token: 'number', foreground: 'b07d3f' },
        { token: 'type', foreground: 'b07d3f' },
        { token: 'function', foreground: '795e26' },
      ],
      colors: {
        'editor.background': '#ffffff',
        'editor.foreground': '#616161',
        'editor.lineHighlightBackground': '#f3f3f3',
        'editor.selectionBackground': 'rgba(176,125,63,0.18)',
        'editorCursor.foreground': '#b07d3f',
        'editorLineNumber.foreground': '#a0a0a0',
        'editorLineNumber.activeForeground': '#616161',
        'editorGutter.background': '#ffffff',
        'editor.selectionHighlightBackground': 'rgba(176,125,63,0.08)',
        'editorBracketMatch.background': 'rgba(176,125,63,0.1)',
        'editorBracketMatch.border': 'rgba(176,125,63,0.3)',
      },
    });
    monaco.editor.setTheme(useStore.getState().theme === 'light' ? 'rx-light' : 'rx-dark');

    // 光标/选区 → store（cursor_line/col + selection {start,end} 1-based，供后端与 StatusBar）
    editor.onDidChangeCursorSelection(() => {
      const pos = editor.getPosition();
      if (pos) {
        setCursorLine(pos.lineNumber);
        setCursorCol(pos.column);
      }
      const sel = editor.getSelection();
      if (sel && !sel.isEmpty()) {
        const lineCount = sel.endLineNumber - sel.startLineNumber + 1;
        const text = editor.getModel().getValueInRange(sel);
        setSelection({ start: sel.startLineNumber, end: sel.endLineNumber }, lineCount, text);
      } else {
        setSelection(null, 0, '');
      }
    });
  }, [setCursorLine, setCursorCol, setSelection]);

  // 清理装饰/视图区的公共函数
  const clearMarks = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return;
    if (decorationsRef.current.length) {
      decorationsRef.current = editor.deltaDecorations(decorationsRef.current, []);
    }
    if (zoneIdsRef.current.length) {
      editor.changeViewZones((acc) => {
        zoneIdsRef.current.forEach((id) => {
          try { acc.removeZone(id); } catch (e) { /* 忽略 */ }
        });
      });
      zoneIdsRef.current = [];
    }
  }, []);

  // 后端 diff 一次性应用：行内装饰（added 绿 / removed 红+删除线）+ 2s 淡化
  useEffect(() => {
    const diff = diffState && diffState.diff;
    if (!ready || !diff || !editorRef.current || !monacoRef.current) return;

    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor.getModel();
    if (!model) return;

    clearTimeout(fadeTimerRef.current);
    clearMarks();

    const lineCount = model.getLineCount();
    const decor = [];
    (diff.added || []).forEach((line) => {
      const ln = Math.max(1, Math.min(lineCount, line | 0));
      decor.push({
        range: new monaco.Range(ln, 1, ln, 1),
        options: {
          isWholeLine: true,
          className: 'rx-diff-add-line',
          glyphMarginClassName: 'rx-diff-add-glyph',
        },
      });
    });

    if (decor.length) {
      decorationsRef.current = editor.deltaDecorations([], decor);
    }

    // removed：{after_line, content} → after_line 行下方插入红色删除线视图区
    const removed = (diff.removed || []).filter((r) => r && typeof r.content === 'string');
    if (removed.length) {
      editor.changeViewZones((acc) => {
        removed.forEach((r) => {
          const anchor = Math.max(0, Math.min(lineCount, r.after_line | 0));
          const div = document.createElement('div');
          div.className = 'rx-zone-remove';
          div.textContent = '- ' + String(r.content).replace(/\s+$/, '');
          try {
            const id = acc.addZone({
              afterLineNumber: anchor,
              heightInLines: 1,
              domNode: div,
            });
            zoneIdsRef.current.push(id);
          } catch (e) { /* 忽略 */ }
        });
      });
    }

    setDiffPanelOpen(true);

    // 2s 后淡化：换用软色装饰并移除视图区，600ms 后彻底清除；变更面板保留至手动关闭
    fadeTimerRef.current = setTimeout(() => {
      const editor2 = editorRef.current;
      const monaco2 = monacoRef.current;
      if (!editor2 || !monaco2) return;
      if (zoneIdsRef.current.length) {
        editor2.changeViewZones((acc) => {
          zoneIdsRef.current.forEach((id) => {
            try { acc.removeZone(id); } catch (e) { /* 忽略 */ }
          });
        });
        zoneIdsRef.current = [];
      }
      const model2 = editor2.getModel();
      if (model2 && decorationsRef.current.length) {
        const soft = (diff.added || []).map((line) => {
          const ln = Math.max(1, Math.min(model2.getLineCount(), line | 0));
          return {
            range: new monaco2.Range(ln, 1, ln, 1),
            options: { isWholeLine: true, className: 'rx-diff-add-line-soft' },
          };
        });
        decorationsRef.current = editor2.deltaDecorations(decorationsRef.current, soft);
      }
      fadeTimerRef.current = setTimeout(clearMarks, 600);
    }, 2000);

    return () => {
      clearTimeout(fadeTimerRef.current);
      clearMarks();
    };
  }, [diffState, ready, setDiffPanelOpen, clearMarks]);

  // 编辑内容：有激活文件条目时同步 dirty，否则仅更新 editorCode
  const handleChange = useCallback((value) => {
    const code = value || '';
    if (useStore.getState().filePath) {
      editActiveEntry(code);
    } else {
      setEditorCode(code);
    }
  }, [editActiveEntry, setEditorCode]);

  // ---------- 文件打开/保存（/api/fs/open|save，与 FileTabs 联动） ----------
  // 页内路径对话框（替代 window.prompt）：{ mode: 'open'|'save', path }
  const [pathDialog, setPathDialog] = useState(null);

  const doOpen = useCallback(async (path) => {
    setBusy(true);
    try {
      const r = await fsOpen(path);
      if (r && r.ok && typeof r.text === 'string') {
        useStore.getState().openFileEntry(path, r.text);
        setToast({ text: '已打开: ' + baseName(path), error: false });
      } else {
        setToast({ text: '打开失败: ' + ((r && r.error) || '未知错误'), error: true });
      }
    } catch (e) {
      setToast({ text: '打开失败: ' + e.message, error: true });
    } finally {
      setBusy(false);
    }
  }, [setToast]);

  const doSave = useCallback(async (path) => {
    setBusy(true);
    try {
      const r = await fsSave(path, useStore.getState().editorCode);
      if (r && r.ok) {
        const st = useStore.getState();
        if (path !== st.filePath) {
          st.openFileEntry(path, st.editorCode);
        } else {
          st.saveActiveEntry();
        }
        setToast({ text: '已保存: ' + baseName(path), error: false });
      } else {
        setToast({ text: '保存失败: ' + ((r && r.error) || '未知错误'), error: true });
      }
    } catch (e) {
      setToast({ text: '保存失败: ' + e.message, error: true });
    } finally {
      setBusy(false);
    }
  }, [setToast]);

  const handleOpen = useCallback(() => {
    setPathDialog({ mode: 'open', path: filePath || '' });
  }, [filePath]);

  const handleSave = useCallback(() => {
    const p = useStore.getState().filePath;
    if (p) {
      doSave(p); // 已有路径直接保存
    } else {
      setPathDialog({ mode: 'save', path: '' });
    }
  }, [doSave]);

  const handleDialogSubmit = useCallback((path) => {
    const d = pathDialog;
    setPathDialog(null);
    const p = (path || '').trim();
    if (!p || !d) return;
    if (d.mode === 'save') doSave(p);
    else doOpen(p);
  }, [pathDialog, doOpen, doSave]);

  // Ctrl+S 保存
  useEffect(() => {
    const el = document.querySelector('.editor-panel');
    if (!el) return;
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && !e.shiftKey && !e.altKey && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        e.stopPropagation();
        handleSave();
      }
    };
    el.addEventListener('keydown', onKey, true);
    return () => el.removeEventListener('keydown', onKey, true);
  }, [handleSave]);

  return (
    <div className={`editor-panel ${controlled ? 'controlled-frame' : ''}`}>
      <div className="editor-main">
        <div className="editor-head">
          <FileTabs />
          <div className="file-tabs-actions">
            <button className="editor-file-btn" onClick={handleOpen} disabled={busy} title="输入路径打开文件">打开</button>
            <button className="editor-file-btn" onClick={handleSave} disabled={busy} title="保存（Ctrl+S）">保存</button>
          </div>
        </div>
        <Breadcrumb />
        <Editor
          height="100%"
          width="100%"
          language={languageOf(filePath)}
          value={editorCode}
          onChange={handleChange}
          onMount={handleEditorMount}
          theme={theme === 'light' ? 'rx-light' : 'rx-dark'}
          options={{
            fontSize: fontSize,
            fontFamily: "'JetBrains Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace",
            minimap: { enabled: false },
            lineNumbers: 'on',
            renderLineHighlight: 'line',
            scrollBeyondLastLine: false,
            automaticLayout: true,
            padding: { top: 8 },
            glyphMargin: true,
            folding: true,
            lineDecorationsWidth: 0,
            bracketPairColorization: { enabled: true },
            wordWrap: 'off',
            smoothScrolling: true,
            cursorBlinking: 'smooth',
            cursorSmoothCaretAnimation: 'on',
            renderWhitespace: 'selection',
            guides: { indentation: true, bracketPairs: true },
            overviewRulerBorder: false,
            hideCursorInOverviewRuler: true,
            overviewRulerLanes: 0,
            tabSize: 2,
            useTabStops: false,
          }}
          loading={<div className="editor-loading">加载编辑器...</div>}
        />
      </div>
      <DiffSidebar open={diffPanelOpen} />

      {pathDialog && (
        <PathDialog
          mode={pathDialog.mode}
          initialPath={pathDialog.path}
          onSubmit={handleDialogSubmit}
          onCancel={() => setPathDialog(null)}
        />
      )}
    </div>
  );
}

/** 页内路径对话框（打开/保存共用，与 SettingsModal 同风格；Enter 提交 / Esc 取消） */
function PathDialog({ mode, initialPath, onSubmit, onCancel }) {
  const [value, setValue] = useState(initialPath || '');
  const inputRef = useRef(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  const submit = () => {
    if (value.trim()) onSubmit(value);
  };
  return (
    <div className="settings-overlay" onClick={onCancel}>
      <div className="settings-modal path-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-header">
          <h2 className="settings-title">{mode === 'save' ? '保存文件' : '打开文件'}</h2>
          <button className="settings-close" onClick={onCancel}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div className="settings-body">
          <div className="settings-group">
            <label className="settings-label">文件路径</label>
            <input
              ref={inputRef}
              className="settings-input"
              type="text"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); submit(); }
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onCancel(); }
              }}
              placeholder="如 H:\share\wt_target\file.py"
              spellCheck={false}
            />
          </div>
        </div>
        <div className="settings-footer">
          <span className="settings-version">Enter 提交 · Esc 取消</span>
          <div className="path-dialog-actions">
            <button className="path-dialog-cancel" onClick={onCancel}>取消</button>
            <button className="settings-save" onClick={submit} disabled={!value.trim()}>
              {mode === 'save' ? '保存' : '打开'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 变更面板：文件名 + 后端 stats（+add/-del），点击打开后端 previews 预览 */
function DiffSidebar({ open }) {
  const diffState = useStore((s) => s.diffState);
  const setDiffPanelOpen = useStore((s) => s.setDiffPanelOpen);
  const setDiffPreview = useStore((s) => s.setDiffPreview);

  if (!open || !diffState) return null;
  const diff = diffState.diff || {};
  const stats = diff.stats || { add: 0, del: 0 };
  const previews = Array.isArray(diff.previews) ? diff.previews : [];

  return (
    <div className="diff-sidebar">
      <div className="diff-sidebar-header">
        <span>变更</span>
        <button className="diff-sidebar-close" onClick={() => setDiffPanelOpen(false)} title="关闭">×</button>
      </div>
      <div className="diff-sidebar-list">
        <div className="diff-file-item diff-file-item-static">
          <span className="diff-file-name">{diffState.name || '未命名'}</span>
          <span className="diff-stats">
            <span className="diff-add">+{stats.add || 0}</span>
            <span className="diff-sep">/</span>
            <span className="diff-remove">-{stats.del || 0}</span>
          </span>
        </div>
        {previews.map((pv, i) => (
          <button
            key={i}
            className="diff-file-item"
            onClick={() => setDiffPreview({ ...pv, file: diffState.name || '未命名' })}
            title={`查看 L${pv.line} 前后对比`}
          >
            <span className="diff-file-name">L{pv.line} 前后对比</span>
            <span className="diff-stats"><span className="diff-add">›</span></span>
          </button>
        ))}
        {previews.length === 0 && (
          <div className="diff-empty">无预览片段</div>
        )}
      </div>
    </div>
  );
}
