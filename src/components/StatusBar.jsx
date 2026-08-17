import { useEffect, useState } from 'react';
import './StatusBar.css';
import useStore from '../store/useStore';

const LANG_BY_EXT = {
  '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript', '.jsx': 'JSX',
  '.tsx': 'TSX', '.html': 'HTML', '.css': 'CSS', '.json': 'JSON',
  '.rs': 'Rust', '.go': 'Go', '.java': 'Java', '.c': 'C', '.h': 'C',
  '.cpp': 'C++', '.cs': 'C#', '.rb': 'Ruby', '.php': 'PHP', '.sh': 'Shell',
  '.sql': 'SQL', '.md': 'Markdown', '.yml': 'YAML', '.yaml': 'YAML',
  '.toml': 'TOML', '.vue': 'Vue', '.lua': 'Lua', '.txt': '纯文本',
};

function langOf(path) {
  if (!path) return '纯文本';
  const dot = path.lastIndexOf('.');
  if (dot < 0) return '纯文本';
  return LANG_BY_EXT[path.slice(dot).toLowerCase()] || '纯文本';
}

/**
 * 窗口最底部 22px 状态栏：
 * 左：文件路径 / 语言 / 行列号（Monaco 光标事件入 store）
 * 右：UTF-8 / CRLF|LF（按编辑器内容探测）/ 后端连接状态（/api/settings 可达即 RX 就绪）
 */
export default function StatusBar() {
  const filePath = useStore((s) => s.filePath);
  const cursorLine = useStore((s) => s.cursorLine);
  const cursorCol = useStore((s) => s.cursorCol);
  const editorCode = useStore((s) => s.editorCode);
  const [online, setOnline] = useState(false);

  // 后端连接探测：挂载即查，30s 复探
  useEffect(() => {
    let alive = true;
    const probe = async () => {
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 3000);
        const r = await fetch('/api/settings', { signal: ctrl.signal });
        clearTimeout(t);
        if (alive) setOnline(r.ok);
      } catch (e) {
        if (alive) setOnline(false);
      }
    };
    probe();
    const timer = setInterval(probe, 30000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const eol = editorCode.includes('\r\n') ? 'CRLF' : 'LF';

  return (
    <div className="status-bar">
      <div className="status-left">
        <span className="status-item status-path" title={filePath || ''}>
          {filePath || '未命名'}
        </span>
        <span className="status-item">{langOf(filePath)}</span>
        <span className="status-item">行 {cursorLine}，列 {cursorCol}</span>
      </div>
      <div className="status-right">
        <span className="status-item">UTF-8</span>
        <span className="status-item">{eol}</span>
        <span className={`status-item status-conn ${online ? 'status-online' : 'status-offline'}`}>
          <span className="status-dot" />
          {online ? 'RX 就绪' : '离线'}
        </span>
      </div>
    </div>
  );
}
