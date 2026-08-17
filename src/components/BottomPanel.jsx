import useStore from '../store/useStore';
import { TelemetryView, LiveFeedView, ToolHotView } from './FeedViews';
import './BottomPanel.css';

/**
 * 底部面板（任务 #16）：三标签 遥测/实时调用流/工具热榜；可折叠；
 * 高度由 App 的 h-resizer 拖拽（80–400，localStorage 持久化）。
 * 注意：底部面板不是终端（终端仅主标签一处）。
 */
export default function BottomPanel() {
  const bottomOpen = useStore((s) => s.bottomOpen);
  const setBottomOpen = useStore((s) => s.setBottomOpen);
  const bottomTab = useStore((s) => s.bottomTab);
  const setBottomTab = useStore((s) => s.setBottomTab);

  return (
    <div className={`bottom-panel ${bottomOpen ? '' : 'bottom-collapsed'}`}>
      <div className="bp-header">
        <div className="bp-tabs">
          {[['telemetry', '遥测'], ['live', '实时调用流'], ['tools', '工具热榜']].map(([k, label]) => (
            <button
              key={k}
              className={`bp-tab ${bottomTab === k && bottomOpen ? 'bp-tab-active' : ''}`}
              onClick={() => {
                if (bottomOpen && bottomTab === k) setBottomOpen(false);
                else setBottomTab(k);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          className="bp-collapse"
          onClick={() => setBottomOpen(!bottomOpen)}
          title={bottomOpen ? '折叠底部面板' : '展开底部面板'}
        >
          {bottomOpen ? '⌄' : '⌃'}
        </button>
      </div>
      {bottomOpen && (
        <div className="bp-content">
          {bottomTab === 'telemetry' && <TelemetryView />}
          {bottomTab === 'live' && <LiveFeedView />}
          {bottomTab === 'tools' && <ToolHotView />}
        </div>
      )}
    </div>
  );
}
