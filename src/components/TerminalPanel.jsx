import { useEffect, useRef, useCallback } from 'react';
import './TerminalPanel.css';
import useStore from '../store/useStore';
import { logTail } from '../utils/ai';

export default function TerminalPanel() {
  const logsEndRef = useRef(null);
  const cursorRef = useRef(0);
  const inFlightRef = useRef(false);
  const terminalLogs = useStore((s) => s.terminalLogs);
  const setCommandInput = useStore((s) => s.setCommandInput);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [terminalLogs]);

  // /api/logtail 增量轮询（3s，cursor 递增；在途请求未完成时跳过本轮，防重入）
  useEffect(() => {
    let timer = null;
    let stopped = false;
    const poll = async () => {
      if (inFlightRef.current || stopped) return;
      inFlightRef.current = true;
      try {
        const r = await logTail(cursorRef.current);
        if (stopped) return;
        if (r && r.ok) {
          if (Array.isArray(r.lines) && r.lines.length) {
            useStore.getState().appendTerminalLines(r.lines);
          }
          cursorRef.current = typeof r.cursor === 'number' ? r.cursor : cursorRef.current;
        }
      } catch (e) { /* 静默，下一轮重试 */ } finally {
        inFlightRef.current = false;
      }
    };
    poll();
    timer = setInterval(poll, 3000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);

  const handleCopy = useCallback((text) => {
    navigator.clipboard.writeText(text).catch(() => {});
  }, []);

  // 右键选中 → 复制并回填命令栏
  const handleContextMenu = useCallback((e) => {
    const sel = window.getSelection()?.toString();
    if (sel) {
      e.preventDefault();
      handleCopy(sel);
      setCommandInput(sel);
    }
  }, [handleCopy, setCommandInput]);

  const logColor = (type) => {
    switch (type) {
      case 'error': return '#e06c75';
      case 'success': return '#4ecb71';
      case 'command': return 'var(--accent)';
      case 'warn': return '#d4a373';
      default: return 'var(--text-secondary)';
    }
  };

  return (
    <div className="terminal-panel">
      <div className="terminal-header">
        <span className="terminal-title">日志终端</span>
        <span className="terminal-hint">只读模式 · 选中文字右键复制到命令栏</span>
      </div>
      <div className="terminal-content" onContextMenu={handleContextMenu}>
        {terminalLogs.map((log, i) => (
          <div key={i} className="terminal-line" style={{ color: logColor(log.type) }}>
            {log.text}
          </div>
        ))}
        <div ref={logsEndRef} />
      </div>
    </div>
  );
}
