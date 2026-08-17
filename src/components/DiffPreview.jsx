import './DiffPreview.css';
import useStore from '../store/useStore';

/**
 * Diff 预览浮层：消费后端 diff.previews 条目 {file, line, before:[行], after:[行]}
 * （前后各 3 行上下文）。ESC / 点击遮罩 / × 关闭（ESC 由 App 全局处理）。
 */
export default function DiffPreview() {
  const diffPreview = useStore((s) => s.diffPreview);
  const setDiffPreview = useStore((s) => s.setDiffPreview);
  if (!diffPreview) return null;

  const { file, line } = diffPreview;
  const before = Array.isArray(diffPreview.before) ? diffPreview.before : [];
  const after = Array.isArray(diffPreview.after) ? diffPreview.after : [];

  return (
    <div className="diff-preview-wrap" onClick={() => setDiffPreview(null)}>
      <div className="diff-preview" onClick={(e) => e.stopPropagation()}>
        <div className="diff-preview-head">
          <span className="diff-preview-file">{file || '未命名'}</span>
          <span className="diff-preview-line">L{line || 1}</span>
          <button className="diff-preview-close" onClick={() => setDiffPreview(null)}>ESC</button>
        </div>
        <div className="diff-preview-body">
          {before.length > 0 && (
            <div className="diff-preview-col">
              <span className="diff-label old">- 修改前</span>
              <pre className="diff-code old-code">{before.join('\n')}</pre>
            </div>
          )}
          {after.length > 0 && (
            <div className="diff-preview-col">
              <span className="diff-label new">+ 修改后</span>
              <pre className="diff-code new-code">{after.join('\n')}</pre>
            </div>
          )}
          {before.length === 0 && after.length === 0 && (
            <div className="diff-preview-empty">该片段无可对比内容</div>
          )}
        </div>
      </div>
    </div>
  );
}
