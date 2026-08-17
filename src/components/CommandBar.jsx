import { useRef, useCallback, useEffect } from 'react';
import './CommandBar.css';
import useStore from '../store/useStore';
import { runCommand, runCommandStream, fetchContext } from '../utils/ai';

/** 文件名提取（变更面板/命令日志用） */
function baseName(p) {
  if (!p) return '未命名';
  return p.split(/[\\/]/).pop() || p;
}

export default function CommandBar() {
  const inputRef = useRef(null);
  const ctxTimerRef = useRef(null);
  const ctxSeqRef = useRef(0);
  const toastTimerRef = useRef(null);

  const commandInput = useStore((s) => s.commandInput);
  const setCommandInput = useStore((s) => s.setCommandInput);
  const selectedLines = useStore((s) => s.selectedLines);
  const contextLines = useStore((s) => s.contextLines);
  const setContextLines = useStore((s) => s.setContextLines);
  const addTerminalLog = useStore((s) => s.addTerminalLog);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const aiLoading = useStore((s) => s.aiLoading);
  const setAiLoading = useStore((s) => s.setAiLoading);
  const aiStatus = useStore((s) => s.aiStatus);
  const setAiStatus = useStore((s) => s.setAiStatus);
  const capturedError = useStore((s) => s.capturedError);
  const setCapturedError = useStore((s) => s.setCapturedError);
  const toast = useStore((s) => s.toast);
  const setToast = useStore((s) => s.setToast);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // 浏览器标签捕获的报错回填命令栏
  useEffect(() => {
    if (capturedError) {
      setCommandInput(`/fix 修复: ${capturedError}`);
      setCapturedError('');
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [capturedError, setCommandInput, setCapturedError]);

  // 临时提示条：4s 自动淡出（错误类 6s），保留手动关闭
  useEffect(() => {
    clearTimeout(toastTimerRef.current);
    if (!toast) return;
    toastTimerRef.current = setTimeout(() => setToast(null), toast.error ? 6000 : 4000);
    return () => clearTimeout(toastTimerRef.current);
  }, [toast, setToast]);

  // 上下文 badge：防抖调 /api/context，请求序号丢弃乱序响应
  useEffect(() => {
    const schedule = () => {
      clearTimeout(ctxTimerRef.current);
      ctxTimerRef.current = setTimeout(async () => {
        const st = useStore.getState();
        const seq = ++ctxSeqRef.current;
        try {
          const r = await fetchContext({
            file_text: st.editorCode,
            cursor_line: st.cursorLine,
            selection: st.selection,
            full: false,
          });
          if (seq !== ctxSeqRef.current) return; // 过期响应丢弃
          if (r && r.ok) setContextLines(r.line_count || 0);
        } catch (e) { /* 静默 */ }
      }, 800);
    };
    schedule();
    const unsub = useStore.subscribe((s, prev) => {
      if (s.editorCode !== prev.editorCode || s.selection !== prev.selection || s.cursorLine !== prev.cursorLine) {
        schedule();
      }
    });
    return () => {
      unsub();
      clearTimeout(ctxTimerRef.current);
    };
  }, [setContextLines]);

  const handleResult = useCallback((cmdText, res) => {
    const st = useStore.getState();
    if (!res || res.ok === false) {
      let msg = (res && res.error) || '命令执行失败';
      if (res && Array.isArray(res.available) && res.available.length) {
        msg += '\n可用命令: ' + res.available.join('  ');
      }
      st.addTerminalLog({ type: 'command', text: `> ${cmdText}` });
      st.addTerminalLog({ type: 'error', text: msg });
      setToast({ text: msg, error: true });
      setActiveTab('terminal');
      return;
    }
    if (res.kind === 'term') {
      // 后端 term 结果字段：stdout_tail / stderr_tail / exit / elapsed_ms（ok=false 时 error/available 已由上方分支处理）
      st.addTerminalLog({ type: 'command', text: '$ ' + cmdText.replace(/^>\s*/, '') });
      if (res.stdout_tail) st.addTerminalLog({ type: 'info', text: String(res.stdout_tail).replace(/\n$/, '') });
      if (res.stderr_tail) st.addTerminalLog({ type: 'error', text: String(res.stderr_tail).replace(/\n$/, '') });
      const exit = typeof res.exit === 'number' ? res.exit : (res.ok ? 0 : 1);
      const ms = typeof res.elapsed_ms === 'number' ? res.elapsed_ms : null;
      st.addTerminalLog({
        type: exit === 0 ? 'success' : 'warn',
        text: 'exit ' + exit + (ms === null ? '' : ' · ' + ms + 'ms'),
      });
      setActiveTab('terminal');
      return;
    }
    if (res.kind === 'edit' && typeof res.new_text === 'string') {
      // 后端 diff 一次性应用：setValue + 行内装饰由 EditorPanel 消费 diffState
      st.setEditorCode(res.new_text);
      st.setDiffState({ name: baseName(st.filePath), diff: res.diff || null });
      st.setDiffPanelOpen(true);
      setActiveTab('editor');
      return;
    }
    // explain / fix 纯文字结果：一次性临时提示条
    setToast({ text: res.reply || '（无返回内容）', error: false });
  }, [setActiveTab, setToast]);

  const handleExecute = useCallback(async (force = false) => {
    const st = useStore.getState();
    const input = st.commandInput.trim();
    if (!input) return;
    if (st.aiLoading && !force) return; // Ctrl+Shift+Enter 强制发送绕过 busy

    setCommandInput('');
    setAiLoading(true);
    setAiStatus('');
    setToast(null);
    setDiffPreviewNull();

    const body = {
      text: input,
      file_path: st.filePath,
      file_text: st.editorCode,
      cursor_line: st.cursorLine,
      selection: st.selection,
      force: !!force, // 后端据此强制 full 上下文
    };

    try {
      let chars = 0;
      // AI 类先走流式；"> " 前缀由后端解析为 term（同样走后端，流式端点单帧 done）
      let res = await runCommandStream(body, (text) => {
        chars += text.length;
        useStore.getState().setAiStatus(`流式接收中 ${chars} 字符`); // 仅进度，不渲染内容
      });
      if (res === null) {
        // 流式失败/error 事件 → 回退非流式
        useStore.getState().setAiStatus('流式失败，回退非流式…');
        res = await runCommand(body);
      }
      handleResult(input, res);
    } catch (err) {
      setToast({ text: '请求失败: ' + err.message, error: true });
    } finally {
      setAiLoading(false);
      setAiStatus('');
      inputRef.current?.focus();
    }
  }, [setCommandInput, setAiLoading, setAiStatus, setToast, handleResult]);

  // 始终指向最新 handleExecute（供只注册一次的全局监听使用）
  const handleExecuteRef = useRef(handleExecute);
  useEffect(() => {
    handleExecuteRef.current = handleExecute;
  }, [handleExecute]);

  // 全局 Ctrl/Cmd+Shift+Enter 强制发送（仅挂载时注册一次，避免每次渲染重绑）
  useEffect(() => {
    const handleGlobalKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.shiftKey && e.key === 'Enter') {
        e.preventDefault();
        handleExecuteRef.current(true);
      }
    };
    window.addEventListener('keydown', handleGlobalKey);
    return () => window.removeEventListener('keydown', handleGlobalKey);
  }, []);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleExecute(e.ctrlKey || e.metaKey);
    }
  }, [handleExecute]);

  const insertPrefix = (p) => {
    setCommandInput(p + ' ');
    inputRef.current?.focus();
  };

  return (
    <>
      {toast && (
        <div className={`rx-toast ${toast.error ? 'rx-toast-error' : ''}`} onClick={() => setToast(null)} title="点击关闭（ESC）">
          <pre className="rx-toast-text">{toast.text}</pre>
          <button className="rx-toast-close" onClick={(e) => { e.stopPropagation(); setToast(null); }}>×</button>
        </div>
      )}

      <div className="command-bar">
        <div className="cmd-prefixes">
          <button className="cmd-prefix-btn" onClick={() => insertPrefix('>')} title="终端命令">{'>'}</button>
          <button className="cmd-prefix-btn" onClick={() => insertPrefix('/explain')} title="解释">/explain</button>
          <button className="cmd-prefix-btn" onClick={() => insertPrefix('/fix')} title="修复">/fix</button>
        </div>

        {selectedLines > 0 && (
          <span className="cmd-selection-badge">[已选 {selectedLines} 行]</span>
        )}

        <div className="cmd-input-wrapper">
          <span className="cmd-prompt">❯</span>
          <input
            ref={inputRef}
            className="cmd-input"
            value={commandInput}
            onChange={(e) => setCommandInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入需求修改代码，或 > 前缀执行命令… Enter 发送 · Ctrl+Shift+Enter 强制"
            spellCheck={false}
          />
        </div>

        <span className="cmd-context-badge">📄 上下文: {contextLines} 行</span>

        {aiLoading && (
          <span className="cmd-loading">
            <span className="cmd-spinner" />
            {aiStatus || '思考中'}
          </span>
        )}

        <button
          className="cmd-send-btn"
          onClick={() => handleExecute(false)}
          disabled={!commandInput.trim() || aiLoading}
          title="Enter 发送"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="19" x2="12" y2="5" />
            <polyline points="5 12 12 5 19 12" />
          </svg>
        </button>
      </div>
    </>
  );
}

/** 发送前关闭 diff 预览浮层 */
function setDiffPreviewNull() {
  useStore.getState().setDiffPreview(null);
}
