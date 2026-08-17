import { useRef } from 'react';
import './BrowserPanel.css';
import useStore from '../store/useStore';

/** 把用户输入规范化为 /preview 内路径（禁止跳出同源反代） */
function toPreviewUrl(input) {
  const v = (input || '').trim();
  if (!v) return '/preview/';
  if (v.startsWith('/preview')) return v;
  if (/^https?:\/\//i.test(v)) {
    // 完整 URL → 取其路径部分经反代（host/port 由 preview_target 决定，忽略输入的来源）
    try {
      const u = new URL(v);
      return '/preview' + (u.pathname || '/') + (u.search || '');
    } catch (e) {
      return '/preview/';
    }
  }
  return '/preview/' + v.replace(/^\/+/, '');
}

export default function BrowserPanel() {
  const iframeRef = useRef(null);
  const urlInputRef = useRef(null);
  const browserUrl = useStore((s) => s.browserUrl);
  const setBrowserUrl = useStore((s) => s.setBrowserUrl);
  const setCapturedError = useStore((s) => s.setCapturedError);
  const setToast = useStore((s) => s.setToast);
  const agentStatus = useStore((s) => s.agentStatus);

  // 受控指示（任务 #16）：agent active 时浏览器标签的 iframe 容器加 2px 绿框
  const controlled = !!(agentStatus && agentStatus.active);

  // 🐞 读取后端注入的 window.__rxErrors（环形数组，最近一条在 [0]）
  const handleCaptureError = () => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    let win = null;
    try {
      win = iframe.contentWindow;
    } catch (e) {
      setToast({ text: '无法访问 iframe（跨域限制）', error: true });
      return;
    }
    if (!win) {
      setToast({ text: '无法访问 iframe', error: true });
      return;
    }
    let errors = null;
    try {
      errors = win.__rxErrors;
    } catch (e) {
      setToast({ text: '跨源页面无法读取错误（请确认走 /preview/ 同源代理）', error: true });
      return;
    }
    if (Array.isArray(errors) && errors.length > 0) {
      setCapturedError(String(errors[0])); // 回填命令栏 /fix 修复:
      setToast({ text: `已捕获最近报错（共 ${errors.length} 条），已回填命令栏`, error: false });
    } else {
      setToast({ text: errors === undefined || errors === null
        ? '页面未注入错误捕获脚本（需经 /preview/ 加载）'
        : '暂无捕获到错误', error: false });
    }
  };

  const handleRefresh = () => {
    const f = iframeRef.current;
    if (f) f.src = browserUrl;
    if (urlInputRef.current) urlInputRef.current.value = browserUrl;
  };

  const handleUrlSubmit = (e) => {
    e.preventDefault();
    if (urlInputRef.current) {
      setBrowserUrl(toPreviewUrl(urlInputRef.current.value));
    }
  };

  return (
    <div className="browser-panel">
      <div className="browser-toolbar">
        <div className="browser-nav">
          <button className="browser-btn" onClick={handleRefresh} title="刷新">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10" />
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
          </button>
          <form className="browser-url-form" onSubmit={handleUrlSubmit}>
            <input
              ref={urlInputRef}
              className="browser-url-input"
              type="text"
              defaultValue={browserUrl}
              placeholder="预览路径（经 /preview/ 同源代理）…"
            />
          </form>
        </div>
        <button className="browser-capture-btn" onClick={handleCaptureError}>
          🐞 捕获报错
        </button>
      </div>
      <div className={`browser-content ${controlled ? 'controlled-frame' : ''}`}>
        <iframe
          ref={iframeRef}
          src={browserUrl}
          className="browser-iframe"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          title="预览"
        />
      </div>
    </div>
  );
}
