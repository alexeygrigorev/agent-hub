/* Private host history UI. Both pages reuse HubMetricsCharts SVG primitives. */
(() => {
  let fetchApi, generation = 0, initialized = false, lastData = null;
  const el = (tag, text, cls) => Object.assign(document.createElement(tag), { textContent: text || '', className: cls || '' });
  const root = () => document.getElementById('host-history');
  let range = '24h', machine = '';
  let plots, status, hostPicker;
  function draw(data) {
    lastData = data;
    plots.replaceChildren();
    const start = Date.parse(data.start), end = Date.parse(data.end);
    status.textContent = `${data.range} · sampled every minute · retained 32 days · older points downsampled · gaps are unknown`;
    if (data.truncated || data.dimension_limit) status.textContent += ' · partial: series limit reached, select a host';
    const all = data.series || [];
    const name = id => (data.machines || []).find(m => m.id === id)?.name || id;
    const hosts = [...new Set(all.map(s => s.machine_id).filter(Boolean))];
    const previous = hostPicker.value;
    if (!machine) {
      hostPicker.replaceChildren(Object.assign(el('option', 'All hosts'), { value: '' }));
      for (const id of hosts) hostPicker.append(Object.assign(el('option', name(id)), { value: id }));
      hostPicker.value = previous;
    }
    const make = (title, rows, unit) => plots.append(HubMetricsCharts.timeSeries(title, rows.map(s => ({ reference: s.reference, label: s.label || `${s.machine_id ? s.machine_id + ' · ' : ''}${s.entity} · ${s.metric}`, points: s.points })), start, end, unit));
    const hostIds = machine ? [machine] : hosts;
    for (const id of hostIds) {
      const h = all.filter(s => s.machine_id === id);
      const memory = h.filter(s => (s.kind === 'host' && s.metric.startsWith('memory_')) || (s.kind === 'reservation' && s.metric === 'reserved_gib')).map(s => ({ ...s, label: s.metric === 'memory_used_gib' ? 'Memory used' : s.metric === 'memory_available_gib' ? 'MemAvailable' : 'Launcher reserved' }));
      if (!memory.some(s => s.metric === 'memory_available_gib' && s.points.some(p => p.value !== null))) {
        memory.push(...h.filter(s => s.kind === 'reservation' && s.metric === 'available_gib').map(s => ({...s,label:'MemAvailable · heartbeat'})));
      }
      memory.push({ reference: true, label: 'MemAvailable floor · 10 GiB', points: [{ at: start, value: 10 }, { at: end, value: 10 }] });
      make(`${name(id)} · Memory and launcher reserve`, memory, ' GiB');
      make(`${name(id)} · Load averages`, h.filter(s => s.kind === 'host' && s.metric.startsWith('load')).map(s => ({ ...s, label: s.metric === 'load1' ? 'Load 1m' : 'Load 15m' })), '');
      make(`${name(id)} · Disk usage per mount`, h.filter(s => s.kind === 'disk').map(s => ({ ...s, label: s.entity })), '%');
      make(`${name(id)} · Sessions`, h.filter(s => s.kind === 'sessions').map(s => ({ ...s, label: s.metric === 'failed' ? 'Failed records' : s.metric })), '');
      const events = h.filter(s => s.kind === 'failures');
      if (events.length) make(`${name(id)} · Recorded failures per minute (backfill)`, events, '');
    }
    const unplaced = all.filter(s => s.kind === 'sessions' && !s.machine_id);
    if (unplaced.length) make('Unassigned session queue', unplaced, '');
    const accounts = [...new Set(all.filter(s => s.kind === 'account' || s.kind === 'quota').map(s => s.entity))];
    for (const id of accounts) {
      const a = all.filter(s => s.entity === id);
      make(`${id} · Account slots used vs max`, a.filter(s => s.kind === 'account').map(s => ({ ...s, label: s.metric === 'used' ? 'Counted slots used' : s.metric === 'max' ? 'Slot limit' : 'Verified native live' })), ' slots');
      make(`${id} · Quota remaining`, a.filter(s => s.kind === 'quota').map(s => ({ ...s, label: 'Quota remaining' })), '%');
    }
    if (!all.length) plots.append(el('p', 'No observations in this range. History accumulates automatically; no measurements are invented.', 'metrics-chart-empty'));
    plots.append(el('p', data.note, 'metrics-chart-note'));
  }
  async function load() {
    const current = ++generation;
    status.textContent = 'Loading resource history…';
    try {
      const data = await fetchApi(`/api/monitoring/history?range=${range}${machine ? '&machine=' + encodeURIComponent(machine) : ''}`);
      if (current === generation) draw(data);
    } catch {
      if (current !== generation) return;
      lastData = null;
      plots.replaceChildren();
      status.textContent = 'Resource history unavailable. Sign in or refresh to retry; measurements unknown.';
    }
  }
  window.HubHostCharts = {
    refresh(api) {
      if (!root()) return;
      fetchApi = api;
      if (!initialized) {
        initialized = true;
        const controls = el('div', '', 'metrics-project-filter');
        const label = el('label', 'History range '), select = el('select', '', 'dk-select');
        select.setAttribute('aria-label', 'Resource history range');
        for (const value of ['1h', '24h', '7d', '30d']) select.append(Object.assign(el('option', value), { value }));
        select.value = range;
        select.addEventListener('change', () => { range = select.value; load(); });
        label.append(select);
        const hostLabel = el('label', 'Host ');
        hostPicker = el('select', '', 'dk-select');
        hostPicker.setAttribute('aria-label', 'Resource history host');
        hostPicker.addEventListener('change', () => { machine = hostPicker.value; load(); });
        hostLabel.append(hostPicker);
        controls.append(label, hostLabel);
        status = el('p', '', 'metrics-chart-note'); status.setAttribute('role', 'status');
        plots = el('div', '', 'host-history-plots');
        root().replaceChildren(controls, status, plots);
        matchMedia('(max-width: 600px)').addEventListener('change', () => { if (lastData) draw(lastData); });
        setInterval(() => { if (!document.hidden && fetchApi) load(); }, 60000);
      }
      return load();
    },
    clear() { generation++; lastData = null; if (plots) plots.replaceChildren(); if (status) status.textContent = 'Sign in to view resource history.'; },
  };
})();
