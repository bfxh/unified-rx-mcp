import { useEffect, useMemo, useRef } from 'react';
import useStore from '../store/useStore';

/** feed 事件按 tool 聚合：次数/平均 wall_ms/总 wall_ms，降序。 */
export function aggregateTools(events) {
  const map = {};
  let total = 0;
  (events || []).forEach((ev) => {
    if (ev.kind !== 'tool' || !ev.tool) return;
    total += 1;
    const a = map[ev.tool] || (map[ev.tool] = { tool: ev.tool, count: 0, total: 0 });
    a.count += 1;
    if (typeof ev.wall_ms === 'number') a.total += ev.wall_ms;
  });
  const rows = Object.values(map).map((a) => ({ ...a, avg: a.count ? a.total / a.count : 0 }));
  rows.sort((x, y) => y.count - x.count);
  return { total, rows };
}

function fmtTs(ts) {
  if (typeof ts !== 'number') return '--:--:--';
  return new Date(ts * 1000).toTimeString().slice(0, 8);
}

/** 实时调用流：行 `ts · tool · status · Nms · args截断`；自动滚底（滚离底暂停）。 */
export function LiveFeedView({ main }) {
  const feedEvents = useStore((s) => s.feedEvents);
  const boxRef = useRef(null);
  const pinRef = useRef(true);

  const onScroll = () => {
    const el = boxRef.current;
    if (el) pinRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  useEffect(() => {
    const el = boxRef.current;
    if (el && pinRef.current) el.scrollTop = el.scrollHeight;
  }, [feedEvents]);

  return (
    <div className={main ? 'feed-main' : 'bp-view'}>
      {main && <div className="feed-main-header">实时调用流<span className="feed-main-hint">2.5s 增量 · 滚离底暂停</span></div>}
      <div className="bp-feed" ref={boxRef} onScroll={onScroll}>
        {feedEvents.length === 0 && <div className="bp-empty">暂无事件（等待 telemetry 写入）</div>}
        {feedEvents.map((ev, i) => (
          <div className="bp-feed-line" key={i}>
            <span className="bp-ts">{fmtTs(ev.ts)}</span>
            <span className="bp-sep">·</span>
            <span className="bp-tool">{ev.tool || ev.kind}</span>
            {ev.status && (
              <>
                <span className="bp-sep">·</span>
                <span className={ev.status === 'ok' ? 'bp-ok' : 'bp-err'}>{ev.status}</span>
              </>
            )}
            {typeof ev.wall_ms === 'number' && (
              <>
                <span className="bp-sep">·</span>
                <span className="bp-ms">{ev.wall_ms}ms</span>
              </>
            )}
            {typeof ev.cycle_ms === 'number' && (
              <>
                <span className="bp-sep">·</span>
                <span className="bp-ms">{ev.cycle_ms}ms</span>
              </>
            )}
            {ev.args && (
              <>
                <span className="bp-sep">·</span>
                <span className="bp-args" title={ev.args}>{String(ev.args).slice(0, 80)}</span>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/** 工具热榜：工具/次数/平均ms/总ms 降序表；顶行总调用数。 */
export function ToolHotView({ main }) {
  const feedEvents = useStore((s) => s.feedEvents);
  const { total, rows } = useMemo(() => aggregateTools(feedEvents), [feedEvents]);
  return (
    <div className={main ? 'feed-main' : 'bp-view'}>
      {main && <div className="feed-main-header">工具热榜</div>}
      <div className="bp-hot-total">总调用 {total}</div>
      <table className="bp-table">
        <thead>
          <tr><th>工具</th><th>次数</th><th>平均 ms</th><th>总 ms</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.tool}>
              <td className="bp-td-tool">{r.tool}</td>
              <td>{r.count}</td>
              <td>{r.avg.toFixed(1)}</td>
              <td>{r.total.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="bp-empty">无工具调用事件</div>}
    </div>
  );
}

/** 遥测统计卡：总调用/成功率/平均 wall_ms/最近心跳 cycle_ms/最后事件距今 s。 */
export function TelemetryView() {
  const feedEvents = useStore((s) => s.feedEvents);
  const agent = useStore((s) => s.agentStatus);

  const stats = useMemo(() => {
    const tools = feedEvents.filter((ev) => ev.kind === 'tool');
    const okN = tools.filter((ev) => ev.status === 'ok').length;
    const wall = tools.reduce((a, e) => a + (typeof e.wall_ms === 'number' ? e.wall_ms : 0), 0);
    const lastHb = [...feedEvents].reverse().find((ev) => ev.kind === 'hb');
    return {
      total: tools.length,
      rate: tools.length ? Math.round((okN / tools.length) * 100) : null,
      avg: tools.length ? (wall / tools.length).toFixed(1) : null,
      hb: lastHb && typeof lastHb.cycle_ms === 'number' ? lastHb.cycle_ms : null,
    };
  }, [feedEvents]);

  return (
    <div className="bp-view bp-tele">
      <div className="bp-card">
        <span className="bp-card-num">{stats.total}</span>
        <span className="bp-card-label">总调用</span>
      </div>
      <div className="bp-card">
        <span className="bp-card-num">{stats.rate === null ? '—' : stats.rate + '%'}</span>
        <span className="bp-card-label">成功率</span>
      </div>
      <div className="bp-card">
        <span className="bp-card-num">{stats.avg === null ? '—' : stats.avg}</span>
        <span className="bp-card-label">平均 wall_ms</span>
      </div>
      <div className="bp-card">
        <span className="bp-card-num">{stats.hb === null ? '—' : stats.hb}</span>
        <span className="bp-card-label">最近心跳 cycle_ms</span>
      </div>
      <div className="bp-card">
        <span className="bp-card-num">{agent ? agent.last_event_age_s + 's' : '—'}</span>
        <span className="bp-card-label">最后事件距今</span>
      </div>
    </div>
  );
}
