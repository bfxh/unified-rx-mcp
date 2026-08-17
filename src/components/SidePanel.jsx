import useStore from '../store/useStore';
import {
  ExplorerPanel, OutlinePanel, TimelinePanel, SearchPanel,
  ScmPanel, TestPanel, RunDebugPanel, ToolCallsPanel,
} from './SidePanels';
import './SidePanel.css';

/** 八面板定义：内联 SVG 小图标 + title */
const PANELS = [
  {
    id: 'explorer', title: '资源管理器', C: ExplorerPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>),
  },
  {
    id: 'outline', title: '大纲', C: OutlinePanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><line x1="9" y1="6" x2="21" y2="6" /><line x1="9" y1="12" x2="21" y2="12" /><line x1="9" y1="18" x2="21" y2="18" /><circle cx="4" cy="6" r="1" /><circle cx="4" cy="12" r="1" /><circle cx="4" cy="18" r="1" /></svg>),
  },
  {
    id: 'timeline', title: '时间线', C: TimelinePanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" /></svg>),
  },
  {
    id: 'search', title: '搜索', C: SearchPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" /></svg>),
  },
  {
    id: 'scm', title: '源代码管理', C: ScmPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" /></svg>),
  },
  {
    id: 'test', title: '测试', C: TestPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M9 3h6" /><path d="M10 3v6L4.5 19a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 9V3" /></svg>),
  },
  {
    id: 'debug', title: '运行和调试', C: RunDebugPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><polygon points="7 4 20 12 7 20" /></svg>),
  },
  {
    id: 'tools', title: '工具调用', C: ToolCallsPanel,
    icon: (<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10" /></svg>),
  },
];

/**
 * 右侧栏（任务 #16）：顶部图标条 8 面板切换 + 收起按钮；
 * 宽度由 App 的 v-resizer 拖拽（160–480，localStorage 持久化）。
 */
export default function SidePanel() {
  const sidePanel = useStore((s) => s.sidePanel);
  const setSidePanel = useStore((s) => s.setSidePanel);
  const setSideCollapsed = useStore((s) => s.setSideCollapsed);

  const active = PANELS.find((p) => p.id === sidePanel) || null;
  const Active = active ? active.C : null;

  return (
    <div className="side-panel">
      <div className="sp-icons">
        {PANELS.map((p) => (
          <button
            key={p.id}
            className={`sp-icon ${sidePanel === p.id ? 'sp-icon-active' : ''}`}
            title={p.title}
            onClick={() => setSidePanel(p.id)}
          >
            {p.icon}
          </button>
        ))}
        <button className="sp-icon sp-collapse-btn" title="收起侧栏" onClick={() => setSideCollapsed(true)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 6 15 12 9 18" />
          </svg>
        </button>
      </div>
      <div className="sp-title">{active ? active.title : '面板'}</div>
      {Active && <Active />}
    </div>
  );
}
