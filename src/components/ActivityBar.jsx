import './ActivityBar.css';
import useStore from '../store/useStore';

/** 内联 SVG 图标（不引第三方图标库） */
const IconFile = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="8" y1="13" x2="16" y2="13" />
    <line x1="8" y1="17" x2="13" y2="17" />
  </svg>
);

const IconGlobe = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <line x1="3" y1="12" x2="21" y2="12" />
    <path d="M12 3a13.5 13.5 0 0 1 0 18a13.5 13.5 0 0 1 0-18z" />
  </svg>
);

const IconTerminal = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="1.5" />
    <polyline points="7 9 10 12 7 15" />
    <line x1="12" y1="15" x2="17" y2="15" />
  </svg>
);

const IconGear = (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

const items = [
  { key: 'editor', label: '编辑器', icon: IconFile },
  { key: 'browser', label: '浏览器', icon: IconGlobe },
  { key: 'terminal', label: '终端', icon: IconTerminal },
];

/**
 * 左侧 48px 活动栏：三视图图标 + 底部齿轮。
 * 与顶部 TabBar 联动同一 store.activeTab，不新增视图语义。
 */
export default function ActivityBar() {
  const activeTab = useStore((s) => s.activeTab);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const setSettingsOpen = useStore((s) => s.setSettingsOpen);

  return (
    <div className="activity-bar">
      <div className="activity-top">
        {items.map((it) => (
          <button
            key={it.key}
            className={`activity-item ${activeTab === it.key ? 'activity-active' : ''}`}
            onClick={() => setActiveTab(it.key)}
            title={it.label}
          >
            {it.icon}
          </button>
        ))}
      </div>
      <div className="activity-bottom">
        <button
          className="activity-item"
          onClick={() => setSettingsOpen(true)}
          title="设置"
        >
          {IconGear}
        </button>
      </div>
    </div>
  );
}
