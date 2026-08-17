import { useState, useEffect } from 'react';
import './SettingsModal.css';
import useStore from '../store/useStore';
import { getSettings, saveSettings } from '../utils/ai';

export default function SettingsModal() {
  const setSettingsOpen = useStore((s) => s.setSettingsOpen);
  const fontSize = useStore((s) => s.fontSize);
  const setFontSize = useStore((s) => s.setFontSize);
  const theme = useStore((s) => s.theme);
  const setTheme = useStore((s) => s.setTheme);

  const [previewTarget, setPreviewTarget] = useState('');
  const [localSize, setLocalSize] = useState(fontSize);
  const [localTheme, setLocalTheme] = useState(theme);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState('');

  // 打开时从后端加载（api_key 为掩码值）
  useEffect(() => {
    let alive = true;
    getSettings().then((r) => {
      if (!alive) return;
      if (r && r.ok && r.data) {
        setPreviewTarget(r.data.preview_target || '');
      } else {
        setErr('读取设置失败: ' + ((r && r.error) || '未知错误'));
      }
    });
    return () => { alive = false; };
  }, []);

  const handleSave = async () => {
    setErr('');
    const patch = {
      preview_target: previewTarget,
      font_size: localSize,
      theme: localTheme,
    };
    const r = await saveSettings(patch);
    if (r && r.ok) {
      setFontSize(localSize);
      setTheme(localTheme);
      setSaved(true);
      setTimeout(() => {
        setSaved(false);
        setSettingsOpen(false);
      }, 400);
    } else {
      setErr('保存失败: ' + ((r && r.error) || '未知错误'));
    }
  };

  const handleOverlayClick = (e) => {
    if (e.target === e.currentTarget) setSettingsOpen(false);
  };

  return (
    <div className="settings-overlay" onClick={handleOverlayClick}>
      <div className="settings-modal">
        <div className="settings-header">
          <h2 className="settings-title">设置</h2>
          <button className="settings-close" onClick={() => setSettingsOpen(false)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="settings-body">
          <div className="settings-group">
            <label className="settings-label">预览目标（preview_target）</label>
            <input
              className="settings-input"
              type="text"
              value={previewTarget}
              onChange={(e) => setPreviewTarget(e.target.value)}
              placeholder="http://localhost:5173"
            />
            <span className="settings-hint">浏览器标签经 /preview/ 同源反代到此地址（仅 http/https）。</span>
          </div>

          <div className="settings-group">
            <label className="settings-label">字体大小</label>
            <div className="settings-row">
              <input
                className="settings-range"
                type="range"
                min="10"
                max="20"
                value={localSize}
                onChange={(e) => {
                  const v = parseInt(e.target.value);
                  setLocalSize(v);
                  setFontSize(v); // 本地即时应用
                }}
              />
              <span className="settings-value">{localSize}px</span>
            </div>
          </div>

          <div className="settings-group">
            <label className="settings-label">主题</label>
            <div className="settings-theme-row">
              <button
                className={`theme-option ${localTheme === 'dark' ? 'theme-active' : ''}`}
                onClick={() => { setLocalTheme('dark'); setTheme('dark'); }}
              >
                <span className="theme-swatch-dark" />
                深色
              </button>
              <button
                className={`theme-option ${localTheme === 'light' ? 'theme-active' : ''}`}
                onClick={() => { setLocalTheme('light'); setTheme('light'); }}
              >
                <span className="theme-swatch-light" />
                浅色
              </button>
            </div>
          </div>

          {err && <div className="settings-error">{err}</div>}
        </div>

        <div className="settings-footer">
          <span className="settings-version">RX-IDE Lite v1.0.0</span>
          <button className={`settings-save ${saved ? 'settings-saved' : ''}`} onClick={handleSave}>
            {saved ? '✓ 已保存' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}
