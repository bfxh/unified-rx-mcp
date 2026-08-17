/**
 * RX-IDE Lite — 后端 API 封装（rxide/host.py，端口 17310，同源）。
 * 不直连任何外部 LLM：命令解析、上下文组装、diff 全部由后端完成。
 */

async function postJson(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json().catch(() => ({ ok: false, error: `响应解析失败 (${r.status})` }));
}

async function getJson(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/** 非流式命令：POST /api/command。 */
export async function runCommand(body) {
  return postJson('/api/command', body);
}

/**
 * 流式命令：POST /api/ai/stream（SSE）。
 * 逐事件回调 onToken(text)；收到 done 返回 result（与 /api/command 完整结果一致）。
 * 收到 error 事件 / 流异常 / 未收到 done → 返回 null，调用方回退 runCommand。
 */
export async function runCommandStream(body, onToken) {
  let resp;
  try {
    resp = await fetch('/api/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return null;
  }
  const ct = resp.headers.get('Content-Type') || '';
  if (!resp.ok || !ct.includes('text/event-stream') || !resp.body) return null;

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (ev.type === 'token') {
          if (onToken) onToken(ev.text || '');
        } else if (ev.type === 'done') {
          return ev.result || { ok: false, error: '空结果' };
        } else if (ev.type === 'error') {
          return null; // 回退非流式
        }
      }
    }
  } catch (e) {
    return null; // 流读取失败 → 回退
  }
  return null; // 未收到 done 即结束 → 回退
}

/** 上下文计算：POST /api/context → {ok, context_text, line_count, fn_name}。 */
export async function fetchContext(body) {
  return postJson('/api/context', body);
}

/** 打开文件：POST /api/fs/open → {ok, text}。 */
export async function fsOpen(path) {
  return postJson('/api/fs/open', { path });
}

/** 保存文件：POST /api/fs/save → {ok}。 */
export async function fsSave(path, text) {
  return postJson('/api/fs/save', { path, text });
}

/** 读设置：GET /api/settings → {ok, data}。 */
export async function getSettings() {
  try {
    const r = await fetch('/api/settings');
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/** 写设置：POST /api/settings（patch）。 */
export async function saveSettings(patch) {
  return postJson('/api/settings', patch);
}

/** 日志增量：GET /api/logtail?cursor=N → {ok, lines, cursor}。 */
export async function logTail(cursor) {
  try {
    const r = await fetch('/api/logtail?cursor=' + (cursor || 0));
    return await r.json();
  } catch (e) {
    return { ok: false, lines: [], cursor: cursor || 0 };
  }
}

// ---------- 任务 #15/16：只读查询端点（侧栏 + 智能体跟踪） ----------

/** 单层目录列举：GET /api/fs/list?path= → {ok, entries, truncated?}。 */
export function fsList(path) {
  return getJson('/api/fs/list?path=' + encodeURIComponent(path || ''));
}

/** 文本搜索：GET /api/search?query=&root= → {ok, hits:[{path,line,text}]}。 */
export function searchFiles(query, root) {
  return getJson(
    '/api/search?query=' + encodeURIComponent(query || '') +
    '&root=' + encodeURIComponent(root || '')
  );
}

/** git status --short：GET /api/git/status?root= → {ok, output}。 */
export function gitStatus(root) {
  return getJson('/api/git/status?root=' + encodeURIComponent(root || ''));
}

/** 智能体事件增量：GET /api/agent/feed?cursor=N → {ok, cursor, reset?, events}。 */
export function agentFeed(cursor) {
  return getJson('/api/agent/feed?cursor=' + (cursor || 0));
}

/** 智能体状态：GET /api/agent/status → {ok, active, last_event_age_s, last_write}。 */
export function agentStatus() {
  return getJson('/api/agent/status');
}
