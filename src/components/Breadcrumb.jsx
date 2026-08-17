import './Breadcrumb.css';
import useStore from '../store/useStore';

/**
 * 编辑器内路径面包屑：按 / 与 \ 拆分段，› 分隔，末级为文件名。
 * Windows 盘符（如 H:）保留为独立段。
 */
export default function Breadcrumb() {
  const filePath = useStore((s) => s.filePath);

  if (!filePath) return null;

  const segs = filePath.split(/[\\/]+/).filter(Boolean);

  return (
    <div className="breadcrumb" title={filePath}>
      {segs.map((seg, i) => (
        <span key={i} className="breadcrumb-seg">
          {i > 0 && <span className="breadcrumb-sep">›</span>}
          <span className={i === segs.length - 1 ? 'breadcrumb-name' : ''}>{seg}</span>
        </span>
      ))}
    </div>
  );
}
