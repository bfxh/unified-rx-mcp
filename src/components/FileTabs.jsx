import './FileTabs.css';
import useStore from '../store/useStore';

function baseName(p) {
  if (!p) return '未命名';
  return p.split(/[\\/]/).pop() || p;
}

/**
 * 编辑器内"已打开文件"横向标签条：
 * 点击切换、× 关闭、未保存显示 ●；与 EditorPanel 打开/保存联动（store.openFiles）。
 */
export default function FileTabs() {
  const openFiles = useStore((s) => s.openFiles);
  const filePath = useStore((s) => s.filePath);
  const switchFileEntry = useStore((s) => s.switchFileEntry);
  const closeFileEntry = useStore((s) => s.closeFileEntry);
  const agentStatus = useStore((s) => s.agentStatus);

  // 受控指示（任务 #16）：agent active 且 last_write.path 命中该标签 → 绿点
  const controlledPath = (agentStatus && agentStatus.active && agentStatus.last_write && agentStatus.last_write.path) || null;

  return (
    <div className="file-tabs">
      {openFiles.length === 0 && (
        <span className="file-tabs-empty">未打开文件</span>
      )}
      {openFiles.map((f) => {
        const active = f.path === filePath;
        return (
          <div
            key={f.path}
            className={`file-tab ${active ? 'file-tab-active' : ''}`}
            onClick={() => switchFileEntry(f.path)}
            title={f.path}
          >
            <span className="file-tab-name">{baseName(f.path)}</span>
            {f.dirty && <span className="file-tab-dirty" title="未保存">●</span>}
            {controlledPath === f.path && (
              <span className="file-tab-controlled" title="受控写入中">●</span>
            )}
            <button
              className="file-tab-close"
              title="关闭"
              onClick={(e) => {
                e.stopPropagation();
                closeFileEntry(f.path);
              }}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
