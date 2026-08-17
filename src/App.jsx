import { useCallback, useEffect } from 'react';
import './App.css';
import useStore from './store/useStore';
import TabBar from './components/TabBar';
import CommandBar from './components/CommandBar';
import StatusBar from './components/StatusBar';
import EditorPanel from './components/EditorPanel';
import BrowserPanel from './components/BrowserPanel';
import TerminalPanel from './components/TerminalPanel';
import SettingsModal from './components/SettingsModal';
import DiffPreview from './components/DiffPreview';
import SidePanel from './components/SidePanel';
import BottomPanel from './components/BottomPanel';
import { LiveFeedView, ToolHotView } from './components/FeedViews';
import { agentFeed, agentStatus } from './utils/ai';

/** 主标签 kind → 面板组件（终端仅主标签一处；底部面板不是终端） */
const panels = {
  editor: EditorPanel,
  browser: BrowserPanel,
  terminal: TerminalPanel,
  live: () => <LiveFeedView main />,
  tools: () => <ToolHotView main />,
};

export default function App() {
  const activeKind = useStore(
    (s) => (s.mainTabs.find((t) => t.id === s.activeMainTabId) || s.mainTabs[0]).kind
  );
  const commandBarVisible = useStore((s) => s.commandBarVisible);
  const toggleCommandBar = useStore((s) => s.toggleCommandBar);
  const settingsOpen = useStore((s) => s.settingsOpen);
  const diffPreview = useStore((s) => s.diffPreview);
  const setDiffPreview = useStore((s) => s.setDiffPreview);
  const toast = useStore((s) => s.toast);
  const setToast = useStore((s) => s.setToast);
  const theme = useStore((s) => s.theme);
  const sideCollapsed = useStore((s) => s.sideCollapsed);
  const sideWidth = useStore((s) => s.sideWidth);
  const bottomOpen = useStore((s) => s.bottomOpen);
  const bottomHeight = useStore((s) => s.bottomHeight);
  const agent = useStore((s) => s.agentStatus);

  // 主题即时生效：html[data-theme] 驱动 CSS 变量组（dark/light）
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const handleKeyDown = useCallback((e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.shiftKey && e.key === 'I') {
      e.preventDefault();
      toggleCommandBar();
    }
    if (e.key === 'Escape') {
      // ESC：先关 diff 预览浮层，再关临时提示条
      if (diffPreview) {
        setDiffPreview(null);
      } else if (toast) {
        setToast(null);
      }
    }
  }, [toggleCommandBar, diffPreview, setDiffPreview, toast, setToast]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  // 智能体 feed 增量轮询（2.5s，字节 cursor；reset 时 store 清空重收）
  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    const poll = async () => {
      if (inFlight || stopped) return;
      inFlight = true;
      try {
        const st = useStore.getState();
        const r = await agentFeed(st.feedCursor);
        if (stopped) return;
        if (r && r.ok) st.ingestFeed(r.events || [], r.cursor || 0, !!r.reset);
      } catch (e) { /* 静默，下一轮重试 */ } finally {
        inFlight = false;
      }
    };
    poll();
    const t = setInterval(poll, 2500);
    return () => { stopped = true; clearInterval(t); };
  }, []);

  // 受控状态轮询（3s）：驱动顶部绿 pill / 浏览器绿框 / 编辑器绿框
  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      if (stopped) return;
      try {
        const r = await agentStatus();
        if (!stopped && r && r.ok) useStore.getState().setAgentStatus(r);
      } catch (e) { /* 静默 */ }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => { stopped = true; clearInterval(t); };
  }, []);

  // ---------- 拖拽分隔条（右栏 160–480 / 底部 80–400，mouseup 持久化） ----------
  const startDrag = (type) => (e) => {
    e.preventDefault();
    const st0 = useStore.getState();
    const d = { type, x: e.clientX, y: e.clientY, w: st0.sideWidth, h: st0.bottomHeight };
    document.body.classList.add(type === 'v' ? 'rx-dragging-v' : 'rx-dragging-h');
    const move = (ev) => {
      const st = useStore.getState();
      if (type === 'v') st.setSideWidth(d.w + (d.x - ev.clientX));
      else st.setBottomHeight(d.h + (d.y - ev.clientY));
    };
    const up = () => {
      const st = useStore.getState();
      if (type === 'v') localStorage.setItem('rx_side_w', String(st.sideWidth));
      else localStorage.setItem('rx_bottom_h', String(st.bottomHeight));
      document.body.classList.remove('rx-dragging-v', 'rx-dragging-h');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };

  const ActivePanel = panels[activeKind] || EditorPanel;

  return (
    <div
      className="app-container"
      style={{ '--side-w': sideWidth + 'px', '--bottom-h': bottomHeight + 'px' }}
    >
      <TabBar />

      <div className="app-body">
        <div className="app-center">
          <div className="panel-container">
            <ActivePanel />
          </div>
          {bottomOpen && <div className="h-resizer" onMouseDown={startDrag('h')} />}
          <BottomPanel />
        </div>

        {!sideCollapsed && <div className="v-resizer" onMouseDown={startDrag('v')} />}
        {!sideCollapsed && <SidePanel />}
      </div>

      {commandBarVisible && <CommandBar />}

      <StatusBar />

      {agent && agent.active && (
        <div className="controlled-pill">受控中 · 元素模式</div>
      )}

      {diffPreview && <DiffPreview />}

      {settingsOpen && <SettingsModal />}
    </div>
  );
}
