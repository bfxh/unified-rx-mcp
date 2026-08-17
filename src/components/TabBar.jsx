import useStore from '../store/useStore';
import './TabBar.css';

/**
 * Edge 式主标签栏（任务 #16）：
 * 主标签 = 编辑器/浏览器/终端/实时调用/工具热榜（可多开编辑器实例），
 * 圆角顶、活动态底色提升、hover 显 ×；右侧 "+" 新开编辑器标签；
 * 切换标签不联动打开其他面板。
 */
export default function TabBar() {
  const mainTabs = useStore((s) => s.mainTabs);
  const activeId = useStore((s) => s.activeMainTabId);
  const activateMainTab = useStore((s) => s.activateMainTab);
  const closeMainTab = useStore((s) => s.closeMainTab);
  const openNewEditorTab = useStore((s) => s.openNewEditorTab);
  const setSettingsOpen = useStore((s) => s.setSettingsOpen);
  const sideCollapsed = useStore((s) => s.sideCollapsed);
  const setSideCollapsed = useStore((s) => s.setSideCollapsed);

  return (
    <div className="tab-bar">
      <div className="tab-bar-tabs">
        {mainTabs.map((t) => (
          <div
            key={t.id}
            className={`edge-tab ${t.id === activeId ? 'edge-tab-active' : ''}`}
            onClick={() => activateMainTab(t.id)}
            title={t.label}
          >
            <span className="edge-tab-label">{t.label}</span>
            {mainTabs.length > 1 && (
              <button
                className="edge-tab-close"
                title="关闭标签"
                onClick={(e) => {
                  e.stopPropagation();
                  closeMainTab(t.id);
                }}
              >
                ×
              </button>
            )}
          </div>
        ))}
        <button className="edge-tab-add" onClick={openNewEditorTab} title="新建编辑器标签">
          +
        </button>
      </div>

      <div className="tab-bar-right">
        <button
          className="tab-icon-btn"
          onClick={() => setSideCollapsed(!sideCollapsed)}
          title={sideCollapsed ? '展开侧栏' : '折叠侧栏'}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <line x1="15" y1="4" x2="15" y2="20" />
          </svg>
        </button>
        <button className="tab-icon-btn" onClick={() => setSettingsOpen(true)} title="设置">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
