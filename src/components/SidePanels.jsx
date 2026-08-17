import { useCallback, useEffect, useMemo, useState } from 'react';
import useStore from '../store/useStore';
import { fsList, fsOpen, searchFiles, gitStatus } from '../utils/ai';
import { revealLine } from '../utils/editorRef';
import { aggregateTools } from './FeedViews';

const DEFAULT_ROOT = 'D:\\开发\\unified-rx-mcp';

function baseName(p) {
  if (!p) return '';
  return p.split(/[\\/]/).pop() || p;
}

function dirOf(p) {
  return (p || '').replace(/[\\/][^\\/]+$/, '');
}

function joinPath(dir, name) {
  if (!dir) return name;
  return /[\\/]$/.test(dir) ? dir + name : dir + '\\' + name;
}

function fmtTs(ts) {
  if (typeof ts !== 'number') return '--:--:--';
  return new Date(ts * 1000).toTimeString().slice(0, 8);
}

/** 当前资源管理器根：打开文件所在目录，无则项目目录 */
function useExplorerRoot() {
  const filePath = useStore((s) => s.filePath);
  return useMemo(() => (filePath ? dirOf(filePath) || DEFAULT_ROOT : DEFAULT_ROOT), [filePath]);
}

/** 打开文件并定位行（侧栏搜索/大纲/scm 共用） */
async function openAndReveal(path, line) {
  const st = useStore.getState();
  const already = st.openFiles.some((f) => f.path === path);
  if (!already) {
    const r = await fsOpen(path);
    if (r && r.ok && typeof r.text === 'string') st.openFileEntry(path, r.text);
    else return;
  } else {
    st.switchFileEntry(path);
  }
  st.setActiveTab('editor'); // 切主标签（不联动其他面板）
  if (line) setTimeout(() => revealLine(line), 150);
}

/* ---------- 1 资源管理器：懒加载树 ---------- */
export function ExplorerPanel() {
  const root = useExplorerRoot();
  const filePath = useStore((s) => s.filePath);
  const [nodes, setNodes] = useState({});
  const [expanded, setExpanded] = useState({});

  const load = useCallback((dir) => {
    fsList(dir).then((r) => {
      if (r && r.ok) setNodes((m) => ({ ...m, [dir]: r.entries || [] }));
    });
  }, []);

  useEffect(() => {
    setNodes({});
    setExpanded({ [root]: true });
    load(root);
  }, [root, load]);

  const toggle = (dir) => {
    setExpanded((m) => {
      const on = !m[dir];
      if (on) load(dir);
      return { ...m, [dir]: on };
    });
  };

  const renderDir = (dir, depth) => {
    const entries = nodes[dir];
    if (!entries) {
      return <div className="sp-row" style={{ paddingLeft: 8 + (depth + 1) * 12, color: 'var(--text-muted)' }}>…</div>;
    }
    return entries.map((en) => {
      const p = joinPath(dir, en.name);
      if (en.type === 'dir') {
        return (
          <div key={p}>
            <button className="sp-row" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => toggle(p)} title={p}>
              <span className={`sp-caret ${expanded[p] ? 'sp-caret-open' : ''}`}>▶</span>
              <span className="sp-name sp-dir">{en.name}</span>
            </button>
            {expanded[p] && renderDir(p, depth + 1)}
          </div>
        );
      }
      return (
        <button
          key={p}
          className={`sp-row ${filePath === p ? 'sp-row-active' : ''}`}
          style={{ paddingLeft: 8 + depth * 12 + 14 }}
          onClick={() => openAndReveal(p)}
          title={p}
        >
          <span className="sp-name">{en.name}</span>
        </button>
      );
    });
  };

  return (
    <div className="sp-scroll">
      <div className="sp-root-title" title={root}>{baseName(root) || root}</div>
      {renderDir(root, 0)}
    </div>
  );
}

/* ---------- 2 大纲：当前文件正则提取 ---------- */
function extractOutline(code, path) {
  const dot = (path || '').lastIndexOf('.');
  const ext = dot >= 0 ? path.slice(dot).toLowerCase() : '';
  const lines = (code || '').split('\n');
  const items = [];
  const pyRe = /^\s*(async\s+def|def|class)\s+([A-Za-z_]\w*)/;
  const jsRe = /^\s*(?:export\s+)?(?:default\s+)?(function|class|const)\s+([A-Za-z_$][\w$]*)/;
  const mdRe = /^(#{1,6})\s+(.+)/;
  lines.forEach((ln, i) => {
    let m = null;
    if (ext === '.py') m = ln.match(pyRe);
    else if (['.js', '.jsx', '.ts', '.tsx', '.mjs'].includes(ext)) m = ln.match(jsRe);
    else m = ln.match(mdRe);
    if (m) {
      const kind = m[1].trim();
      const name = m[2] || '';
      items.push({ line: i + 1, label: (kind.includes('def') || kind === 'class' || kind === 'function' || kind === 'const' ? kind + ' ' : '') + name });
    }
  });
  return items;
}

export function OutlinePanel() {
  const editorCode = useStore((s) => s.editorCode);
  const filePath = useStore((s) => s.filePath);
  const items = useMemo(() => extractOutline(editorCode, filePath), [editorCode, filePath]);
  const go = (line) => {
    useStore.getState().setActiveTab('editor');
    setTimeout(() => revealLine(line), 150);
  };
  return (
    <div className="sp-scroll">
      {items.length === 0 && <div className="sp-empty">无符号（py: def/class；js/ts: function/const/class；md: 标题）</div>}
      {items.map((it, i) => (
        <button key={i} className="sp-row" style={{ paddingLeft: 8 }} onClick={() => go(it.line)}>
          <span className="sp-sym">ƒ</span>
          <span className="sp-name">{it.label}</span>
          <span className="sp-line">:{it.line}</span>
        </button>
      ))}
    </div>
  );
}

/* ---------- 3 时间线：当前文件相关事件 + mtime 首行 ---------- */
export function TimelinePanel() {
  const feedEvents = useStore((s) => s.feedEvents);
  const filePath = useStore((s) => s.filePath);
  const [mtime, setMtime] = useState(null);

  useEffect(() => {
    let alive = true;
    setMtime(null);
    if (!filePath) return undefined;
    fsList(dirOf(filePath)).then((r) => {
      if (!alive) return;
      const name = baseName(filePath);
      const en = ((r && r.ok && r.entries) || []).find((e) => e.name === name);
      if (en) setMtime(en.mtime);
    });
    return () => { alive = false; };
  }, [filePath]);

  const rows = useMemo(
    () => (filePath ? feedEvents.filter((ev) => ev.args && ev.args.includes(filePath)).slice().reverse() : []),
    [feedEvents, filePath]
  );

  return (
    <div className="sp-scroll">
      {!filePath && <div className="sp-empty">未打开文件</div>}
      {filePath && (
        <div className="sp-row sp-row-static" style={{ paddingLeft: 8 }}>
          <span className="sp-dot" style={{ background: 'var(--accent)' }} />
          <span className="sp-name">最后修改 {mtime ? fmtTs(mtime) : '—'}</span>
        </div>
      )}
      {rows.map((ev, i) => (
        <div key={i} className="sp-row sp-row-static" style={{ paddingLeft: 8 }}>
          <span className={`sp-dot ${ev.status === 'error' ? 'sp-dot-err' : 'sp-dot-ok'}`} />
          <span className="sp-line">{fmtTs(ev.ts)}</span>
          <span className="sp-name" title={ev.args || ''}>{ev.tool || ev.kind}</span>
        </div>
      ))}
      {filePath && rows.length === 0 && <div className="sp-empty">本会话无该文件相关事件</div>}
    </div>
  );
}

/* ---------- 4 搜索 ---------- */
export function SearchPanel() {
  const root = useExplorerRoot();
  const [q, setQ] = useState('');
  const [hits, setHits] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!q.trim() || busy) return;
    setBusy(true);
    const r = await searchFiles(q, root);
    setHits(r && r.ok ? r.hits || [] : []);
    setBusy(false);
  };

  return (
    <div className="sp-col">
      <div className="sp-search-box">
        <input
          className="sp-input"
          type="text"
          value={q}
          placeholder={'搜索（回车，根=' + baseName(root) + '）'}
          spellCheck={false}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') run(); }}
        />
      </div>
      <div className="sp-scroll">
        {hits === null && <div className="sp-empty">输入关键词回车搜索</div>}
        {hits && hits.length === 0 && <div className="sp-empty">无命中</div>}
        {(hits || []).map((h, i) => (
          <button key={i} className="sp-row sp-row-wrap" style={{ paddingLeft: 8 }} onClick={() => openAndReveal(h.path, h.line)} title={h.path}>
            <span className="sp-name sp-hit-file">{baseName(h.path)}:{h.line}</span>
            <span className="sp-hit-text">{h.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------- 5 源代码管理 ---------- */
export function ScmPanel() {
  const root = useExplorerRoot();
  const [out, setOut] = useState(null);
  const load = useCallback(() => { gitStatus(root).then((r) => setOut(r || { ok: false })); }, [root]);
  useEffect(() => { load(); }, [load]);

  const rows = useMemo(() => {
    if (!out || !out.ok) return [];
    return (out.output || '').split('\n').filter((l) => l.trim()).map((line) => {
      const m = line.match(/^(\S{1,2})\s+(.*)$/);
      let code = m ? m[1] : '??';
      let p = m ? m[2] : line.trim();
      if (p.includes(' -> ')) p = p.split(' -> ').pop(); // 重命名取新路径
      return { code, path: p, full: joinPath(root, p) };
    });
  }, [out, root]);

  return (
    <div className="sp-col">
      <div className="sp-scm-head">
        <span className="sp-scm-title">更改{rows.length ? ` (${rows.length})` : ''}</span>
        <button className="sp-refresh" onClick={load} title="刷新">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
        </button>
      </div>
      <div className="sp-scroll">
        {out && !out.ok && <div className="sp-empty">git 不可用: {(out.error || '').slice(0, 80)}</div>}
        {rows.length === 0 && out && out.ok && <div className="sp-empty">工作区干净</div>}
        {rows.map((r, i) => (
          <button key={i} className="sp-row" style={{ paddingLeft: 8 }} onClick={() => openAndReveal(r.full)} title={r.full}>
            <span className={`sp-scm-code ${r.code.includes('?') ? 'sp-scm-untracked' : ''}`}>{r.code}</span>
            <span className="sp-name">{r.path}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------- 6 测试 ---------- */
const TEST_TOOLS = new Set(['bug_scan', 'std_check', 'vuln_scan', 'ui_check']);

export function TestPanel() {
  const feedEvents = useStore((s) => s.feedEvents);
  const rows = useMemo(
    () => feedEvents.filter((ev) => ev.kind === 'tool' && TEST_TOOLS.has(ev.tool)).slice().reverse(),
    [feedEvents]
  );
  return (
    <div className="sp-scroll">
      {rows.length === 0 && <div className="sp-empty">本会话无测试类事件（bug_scan/std_check/vuln_scan/ui_check）</div>}
      {rows.map((ev, i) => (
        <div key={i} className="sp-row sp-row-static" style={{ paddingLeft: 8 }} title={ev.args || ''}>
          <span className={`sp-dot ${ev.status === 'error' ? 'sp-dot-err' : 'sp-dot-ok'}`} />
          <span className="sp-line">{fmtTs(ev.ts)}</span>
          <span className="sp-name">{ev.tool} {(ev.args || '').slice(0, 48)}</span>
        </div>
      ))}
    </div>
  );
}

/* ---------- 7 运行和调试 ---------- */
export function RunDebugPanel() {
  const feedEvents = useStore((s) => s.feedEvents);
  const terminalLogs = useStore((s) => s.terminalLogs);
  const hbs = useMemo(() => feedEvents.filter((ev) => ev.kind === 'hb').slice().reverse(), [feedEvents]);
  const cmds = useMemo(
    () => terminalLogs.filter((l) => (l.text || '').startsWith('$')).slice().reverse(),
    [terminalLogs]
  );
  return (
    <div className="sp-scroll">
      <div className="sp-section">心跳（hb）</div>
      {hbs.length === 0 && <div className="sp-empty">无心跳事件</div>}
      {hbs.slice(0, 60).map((ev, i) => (
        <div key={i} className="sp-row sp-row-static" style={{ paddingLeft: 8 }}>
          <span className="sp-dot sp-dot-ok" />
          <span className="sp-line">{fmtTs(ev.ts)}</span>
          <span className="sp-name">loop={ev.loop ?? '?'} · {ev.cycle_ms ?? '?'}ms</span>
        </div>
      ))}
      <div className="sp-section">本会话终端命令</div>
      {cmds.length === 0 && <div className="sp-empty">终端尚未执行命令</div>}
      {cmds.slice(0, 60).map((l, i) => (
        <div key={i} className="sp-row sp-row-static" style={{ paddingLeft: 8 }}>
          <span className="sp-name sp-cmd">{l.text}</span>
        </div>
      ))}
    </div>
  );
}

/* ---------- 8 工具调用 ---------- */
export function ToolCallsPanel() {
  const feedEvents = useStore((s) => s.feedEvents);
  const { total, rows } = useMemo(() => aggregateTools(feedEvents), [feedEvents]);
  return (
    <div className="sp-scroll">
      <div className="sp-root-title">总调用 {total}</div>
      {rows.length === 0 && <div className="sp-empty">无工具调用事件</div>}
      {rows.map((r) => (
        <div key={r.tool} className="sp-row sp-row-static" style={{ paddingLeft: 8 }}>
          <span className="sp-name sp-dir">{r.tool}</span>
          <span className="sp-line">{r.count}次 · 均{r.avg.toFixed(1)}ms · 总{r.total.toFixed(0)}ms</span>
        </div>
      ))}
    </div>
  );
}
