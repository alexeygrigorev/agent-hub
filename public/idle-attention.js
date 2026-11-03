/* Compact sessions summary for the home landing page; private API data stays text-only. */
(() => {
  const root = document.getElementById('session-attention');
  if (!root) return;
  const category = row => {
    if (['waiting_confirmation', 'waiting_for_human', 'needs_human'].includes(row.state)) return 'Waiting for you';
    if (['stalled', 'blocked'].includes(row.state)) return 'Blocked';
    if (['idle_with_open_ac', 'idle'].includes(row.state)) return 'Idle with unfinished work';
    if (row.source === 'session_ping' || row.state === 'uncertain' || row.source === 'session_uncertain') return 'Stale observation';
    return 'Needs attention';
  };
  async function refresh() {
    try {
      const [statusResponse, sessionsResponse] = await Promise.all([
        fetch('/api/status', { credentials: 'same-origin', cache: 'no-store' }),
        fetch('/api/sessions?limit=1&live=1', { credentials: 'same-origin', cache: 'no-store' })
      ]);
      if (!statusResponse.ok || !sessionsResponse.ok) throw new Error('unavailable');
      const [status, sessions] = await Promise.all([statusResponse.json(), sessionsResponse.json()]);
      if (!Array.isArray(status.needs_attention) || !Number.isFinite(sessions.active_count)) throw new Error('invalid');
      const relevant = status.needs_attention.filter(row => ['session_idle', 'session_ping', 'session_stalled', 'session_uncertain'].includes(row.source));
      const count = document.getElementById('session-attention-count');
      if (count) count.textContent = `${relevant.length} ${relevant.length === 1 ? 'session' : 'sessions'}`;
      const running = document.getElementById('overview-running-count');
      if (running) running.textContent = `${sessions.active_count} active`;
      root.replaceChildren();
      if (!relevant.length) {
        const empty = document.createElement('p'); empty.className = 'dk-muted'; empty.textContent = 'No sessions need attention.'; root.append(empty); return;
      }
      for (const row of relevant.slice(0, 6)) {
        const item = document.createElement('article'); item.className = 'sessions-attention-item';
        const link = document.createElement('a'); link.className = 'sessions-attention-item__title';
        link.href = '/sessions.html?session=' + encodeURIComponent(row.session_id || '');
        link.textContent = row.intake_id ? `Task ${row.intake_id}` : row.title || `Session ${row.session_id || ''}`;
        const label = document.createElement('span'); label.className = 'dk-badge dk-badge--warning'; label.textContent = category(row);
        const context = document.createElement('span'); context.className = 'dk-muted'; context.textContent = row.intake_id ? `Linked task ${row.intake_id}` : 'Open the session to review its status.';
        item.append(link, label, context); root.append(item);
      }
      if (relevant.length > 6) {
        const more = document.createElement('a'); more.href = '/sessions.html?view=attention'; more.textContent = `See all ${relevant.length} sessions needing attention`; root.append(more);
      }
      const statusLine = document.getElementById('status'); if (statusLine) statusLine.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch {
      root.textContent = 'Session attention is unavailable. Sign in with operator access or retry.';
      const count = document.getElementById('session-attention-count'); if (count) count.textContent = 'Unavailable';
      const running = document.getElementById('overview-running-count'); if (running) running.textContent = 'Unavailable';
    }
  }
  refresh();
  // Intake 94186660: hidden tabs skip the 60s poll.
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
})();
