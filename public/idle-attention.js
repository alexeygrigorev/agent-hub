/* Canonical status projection; text-only rendering of private session metadata. */
(() => {
  const root = document.getElementById('session-attention');
  if (!root) return;
  async function refresh() {
    try {
      const response = await fetch('/api/status', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Array.isArray(data.needs_attention)) throw new Error('invalid');
      const sessions = data.needs_attention.filter(row => row.source === 'session_idle');
      root.replaceChildren();
      if (!sessions.length) { root.textContent = 'No stalled sessions reported.'; return; }
      const table = document.createElement('table');
      table.className = 'dk-table';
      const header = document.createElement('tr');
      for (const label of ['Session', 'Intake', 'State', 'Idle seconds', 'Last activity', 'Last nudge']) {
        const cell = document.createElement('th'); cell.textContent = label; header.append(cell);
      }
      table.append(header);
      for (const session of sessions) {
        const row = document.createElement('tr');
        for (const value of [session.session_id, session.intake_id || 'Unlinked', session.state,
          Number.isFinite(session.idle_for) ? Math.floor(session.idle_for) : 'Unknown',
          session.last_activity_at || 'Unknown', session.last_nudge || 'None']) {
          const cell = document.createElement('td'); cell.textContent = String(value); row.append(cell);
        }
        table.append(row);
      }
      root.append(table);
    } catch { root.textContent = 'Session attention unavailable. Sign in with operator access or retry.'; }
  }
  refresh();
  setInterval(refresh, 60000);
})();
