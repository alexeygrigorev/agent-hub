/* Private, read-only statistics. All task text is rendered with textContent. */
if (/^#\/intake\/[A-Za-z0-9._:-]+$/.test(location.hash)) location.replace('/intake/' + encodeURIComponent(location.hash.slice(9)));
window.intakeStats = (() => {
  const fields = [
    ['created_at', 'Added'], ['first_session_started_at', 'First session started'], ['done_at', 'DONE'],
    ['lead_time_ms', 'Lead time'], ['cycle_time_ms', 'Cycle time'],
    ['total_session_wall_time_ms', 'Session wall time'], ['total_session_active_time_ms', 'Active time'],
    ['session_count', 'Started sessions'], ['attempt_count', 'Retained attempts'], ['failed_attempts', 'Failed'],
    ['stopped_attempts', 'Stopped / cancelled'], ['restarted_attempts', 'Restarted'],
    ['engines', 'Engines'], ['accounts', 'Accounts'], ['blocker_count', 'Blockers']
  ];
  const duration = n => n === null ? 'Unknown' : n < 60000 ? `${Math.round(n / 1000)}s` : n < 3600000 ? `${(n / 60000).toFixed(1)}m` : `${(n / 3600000).toFixed(1)}h`;
  function value(s, key) {
    const v = s[key];
    if (v == null) return 'Unknown · ' + (s.unknown_reasons?.[key] || 'not retained').replaceAll('_', ' ');
    if (key.endsWith('_at')) return new Date(v).toLocaleString();
    if (key.endsWith('_ms')) return duration(v);
    if (Array.isArray(v)) return (v.join(', ') || 'None recorded') + (s.unknown_reasons?.[key] ? ' · incomplete' : '');
    return String(v);
  }
  function detail(s) {
    const node = document.getElementById('intake-detail-stats');
    if (!node) return;
    node.replaceChildren();
    if (!s) { node.textContent = 'Statistics unavailable'; return; }
    const dl = document.createElement('dl'); dl.className = 'session-meta';
    for (const [key, label] of fields) {
      const item = document.createElement('div'); item.className = 'session-meta-item';
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = value(s, key); dd.style.margin = '0';
      item.append(dt, dd); dl.append(item);
    }
    node.append(dl);
    const basis = document.createElement('p'); basis.className = 'dk-muted';
    basis.textContent = [s.count_basis, s.wall_time_basis].filter(Boolean).join(' '); node.append(basis);
  }
  async function table() {
    const node = document.getElementById('intake-stats-table');
    if (!node) return;
    const params = new URLSearchParams({window: document.getElementById('stats-window').value, sort: document.getElementById('stats-sort').value, order: 'desc'});
    try {
      const response = await fetch('/api/intake/stats?' + params, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json(); node.replaceChildren();
      const summary = document.createElement('p');
      summary.textContent = `DONE: ${data.aggregates.done_count ?? 'Unknown'} · Median lead: ${duration(data.aggregates.median_lead_time_ms)} · Attempts per DONE: ${data.aggregates.attempts_per_done ?? 'Unknown'}`;
      const basis = document.createElement('p'); basis.className = 'dk-muted';
      basis.textContent = data.aggregates.window_basis + ' ' + Object.entries(data.aggregates.unknown_reasons ?? {}).map(([key,reason]) => `${key}: ${reason}`.replaceAll('_',' ')).join(' · ');
      const top = document.createElement('p'); top.textContent = 'Top by attempts: ' + (data.aggregates.top_intakes_by_attempts.map(t => `${t.title} (${t.attempt_count})`).join(' · ') || 'None in window');
      const wrap = document.createElement('div'); wrap.style.overflowX = 'auto'; wrap.tabIndex = 0; wrap.setAttribute('aria-label', 'Scrollable intake statistics');
      const table = document.createElement('table'); table.className = 'dk-table';
      const head = document.createElement('thead'), row = document.createElement('tr');
      const first = ['attempt_count', 'lead_time_ms', 'cycle_time_ms', 'blocker_count'];
      const columns = [['title', 'Intake'], ...first.map(k => fields.find(f => f[0] === k)), ...fields.filter(f => !first.includes(f[0]))];
      for (const [, title] of columns) { const th = document.createElement('th'); th.textContent = title; th.scope = 'col'; row.append(th); }
      head.append(row); table.append(head); const body = document.createElement('tbody');
      for (const s of data.items) {
        const tr = document.createElement('tr');
        for (const [key] of columns) {
          const td = document.createElement('td');
          if (key === 'title') { const a = document.createElement('a'); a.href = '/intake/' + encodeURIComponent(s.id); a.textContent = s.title; td.append(a); }
          else td.textContent = value(s, key);
          tr.append(td);
        }
        body.append(tr);
      }
      table.append(body); wrap.append(table); node.append(summary, basis, top, wrap);
    } catch { node.textContent = 'Statistics unavailable. Sign in or retry.'; }
  }
  document.addEventListener('DOMContentLoaded', () => {
    for (const id of ['stats-window', 'stats-sort']) document.getElementById(id)?.addEventListener('change', table);
    table();
  });
  return { detail, table };
})();
