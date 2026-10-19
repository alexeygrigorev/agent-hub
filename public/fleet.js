/* Render server data as text; never inject HTML, transcripts or credentials. */
const byId = id => document.getElementById(id);
let csrf = null;
let refreshing = false;
const age = at => {
  const value = Date.parse(at);
  return Number.isFinite(value) ? Date.now() - value : Infinity;
};
const fresh = at => age(at) >= 0 && age(at) <= 180000;
function table(columns, rows) {
  const wrapper = document.createElement('div'); wrapper.className = 'scroll';
  const t = document.createElement('table'); t.className = 'dk-table'; const head = document.createElement('tr');
  for (const title of columns) { const th = document.createElement('th'); th.scope = 'col'; th.textContent = title; head.append(th); }
  const thead = document.createElement('thead'); thead.append(head); t.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) { const tr = document.createElement('tr'); for (const [index, text] of row.entries()) { const td = document.createElement('td'); td.dataset.label = columns[index]; td.textContent = text ?? 'unknown'; tr.append(td); } body.append(tr); }
  if (!rows.length) { const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = columns.length; cell.textContent = 'No records reported.'; row.append(cell); body.append(row); }
  t.append(body); wrapper.append(t); return wrapper;
}
async function api(path, method = 'GET', data) {
  const headers = {};
  if (data !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf; }
  const r = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', body: data === undefined ? undefined : JSON.stringify(data) });
  if (r.status === 401) { csrf = null; for (const b of document.querySelectorAll('form button, #checks button')) b.disabled = true; location.assign('/auth/login'); throw Error('Sign-in required'); }
  if (!r.ok) throw Error(`Hub request failed (${r.status}); request may need reconciliation.`);
  return r.json();
}
function render(data) {
  const root = byId('machine-view'); root.replaceChildren();
  for (const m of data.machines) {
    const section = document.createElement('article'); section.className = 'machine';
    const title = document.createElement('h3'); title.textContent = `${m.name || m.id} · ${m.drained ? 'drained' : fresh(m.last_checkin_at) ? 'online' : 'stale / offline'}`; section.append(title);
    const subscriptions = data.subscriptions.filter(s => s.machine_id === m.id);
    section.append(table(['Service', 'Account label', 'Plan', 'Quota / used allowance / reset'], subscriptions.map(s => [s.service, s.account, s.plan, `${s.stale ? 'stale; ' : ''}${s.quota.status}; ` + Object.entries(s.quota.windows || {}).map(([name, w]) => `${name}: ${w.present ? `${w.percent_remaining ?? 'unknown'}% left / ${w.percent_remaining === null ? 'unknown' : 100 - w.percent_remaining}% used; reset ${w.reset_at || 'unknown'}` : 'not present'}`).join(' | ')])));
    if (!subscriptions.length) { const p = document.createElement('p'); p.textContent = 'No subscription report. Configure this machine and enable its metadata collector.'; section.append(p); }
    root.append(section);
  }
  root.append(table(['Shared account', 'Service', 'Status', 'Remaining', 'Active / limit', 'Machines'], data.accounts.map(a => [a.label, a.provider, a.status, a.remaining_percent === null ? 'unknown' : `${a.remaining_percent}%`, `${a.active} / ${a.max_concurrency}`, a.machines.join(', ')])));
  const map = new Map(data.launches.map(a => [a.id, a]));
  const depth = launch => { const seen = new Set(); let p = launch, n = 0; while (p.parent_id && map.has(p.parent_id) && !seen.has(p.parent_id) && n < 20) { seen.add(p.parent_id); p = map.get(p.parent_id); n++; } return n; };
  byId('hub-agents').replaceChildren(table(['Agent / parent', 'Kind', 'Machine', 'Mode', 'Requested by / when', 'Task', 'State / native reference'], data.launches.map(a => [`${'↳ '.repeat(depth(a))}${a.id}${a.parent_id ? `; parent ${a.parent_id}` : '; root request'}`, `${a.engine} / ${a.profile || 'default'} / ${a.account}`, a.machine_id, a.mode, `${a.actor} (${a.requester_kind}) / ${a.created_at}`, a.summary, `${a.state} / ${a.native_id || 'none yet'}`])));
  const machineName = id => data.machines.find(m => m.id === id)?.name || id;
  byId('sessions').replaceChildren(table(['Machine', 'Workspace', 'Tag', 'Engine / profile / account', 'Status', 'Last change'], data.sessions.map(s => [machineName(s.machine_id), s.workspace, s.tag, `${s.engine || 'shell'} / ${s.profile || 'default'} / ${s.account || 'unknown'}`, `${s.phase} / ${s.agent_state || 'unknown'}`, `${s.observed_at}${fresh(data.machines.find(m => m.id === s.machine_id)?.last_checkin_at) ? '' : ' (machine not reporting)'}`])));
  byId('registered-agents').replaceChildren(table(['Agent', 'Project / role', 'Machine', 'Engine', 'Session', 'Health'], data.agents.map(a => [a.tag, `${a.project} / ${a.role}`, a.machine_id, a.engine, a.session_id, `${a.health_state || 'unknown'} / ${a.health_at || 'no report'}`])));
  const select = byId('machines'); const selected = select.value;
  select.replaceChildren(new Option('Choose automatically', ''));
  for (const m of data.machines) select.append(new Option(m.name || m.id, m.id));
  select.value = selected;
}
const PROBLEM = new Set(['stale', 'offline', 'stalled', 'stuck', 'uncertain', 'missing', 'fail', 'error']);
function stateCell(state) { const span = document.createElement('span'); span.className = 'dk-badge ' + (PROBLEM.has(state) ? 'dk-badge--danger' : ['online', 'working'].includes(state) ? 'dk-badge--success' : 'dk-badge--neutral'); span.textContent = state; return span; }
function quotaBadge(state) {
  const span = document.createElement('span');
  span.className = 'dk-badge ' + (state === 'fresh' ? 'dk-badge--success' : ['exhausted', 'unavailable', 'stale'].includes(state) ? 'dk-badge--danger' : 'dk-badge--neutral');
  span.textContent = state || 'unknown';
  return span;
}
function quotaWindowState(snap, w) {
  if (!snap || snap.stale) return 'stale';
  if (snap.status === 'error') return 'unavailable';
  if (snap.status !== 'ok') return 'unknown';
  if (w && w.error === 'unavailable') return 'unavailable';
  if (!w || w.present !== true) return 'unknown';
  return 'fresh';
}
function nativeText(w) {
  if (!w) return 'unknown';
  const parts = [];
  if (w.credits_remaining !== null && w.credits_remaining !== undefined) parts.push(`${w.credits_remaining} credits`);
  if (w.spend_used !== null && w.spend_used !== undefined) parts.push(`spent ${w.spend_used}${w.unit ? ' ' + w.unit : ''}`);
  if (w.spend_limit !== null && w.spend_limit !== undefined) parts.push(`limit ${w.spend_limit}${w.unit ? ' ' + w.unit : ''}`);
  if (w.profile) parts.push(`profile ${w.profile}`);
  if (parts.length) return parts.join('; ');
  return w.unit && w.unit !== 'percent' ? w.unit : 'percent';
}
function renderQuota(q) {
  const root = byId('quota-panel'); root.replaceChildren();
  if (!q || q.error) {
    const p = document.createElement('p'); p.textContent = q && q.error ? `Quota unavailable: ${q.error}` : 'Quota unavailable: no data.'; root.append(p); return;
  }
  const pools = Array.isArray(q.pools) ? q.pools : [];
  root.append(richTable(['Provider', 'Account', 'State', 'Remaining', 'Observed', 'Hosts'],
    pools.map(p => [p.provider, p.account, quotaBadge(p.state),
      p.remaining_percent === null || p.remaining_percent === undefined ? 'unknown' : `${p.remaining_percent}%`,
      p.observed_at || 'never',
      Array.isArray(p.observed_by_hosts) && p.observed_by_hosts.length ? p.observed_by_hosts.join(', ') : 'no host'])));
  const cov = q.coverage || {};
  const cp = document.createElement('p');
  cp.textContent = `Coverage: ${((cov.fresh || []).length)} fresh, ${((cov.stale || []).length)} stale, ${((cov.missing || []).length)} missing of ${cov.machines_total ?? '?'} machines.`;
  root.append(cp);
  const winRows = [];
  for (const h of Array.isArray(q.hosts) ? q.hosts : []) {
    const snap = h.snapshot || {};
    const routes = snap.routes && typeof snap.routes === 'object' ? snap.routes : {};
    for (const [provider, route] of Object.entries(routes)) {
      const windows = route && typeof route.windows === 'object' ? route.windows : {};
      for (const [name, w] of Object.entries(windows)) {
        const pct = !w || w.percent_remaining === null || w.percent_remaining === undefined ? 'unknown' : `${w.percent_remaining}%`;
        winRows.push([h.host_id, provider, name, quotaBadge(quotaWindowState(snap, w)), pct, nativeText(w), (w && w.reset_at) || 'unknown']);
      }
    }
    for (const s of Array.isArray(h.subscriptions) ? h.subscriptions : []) {
      if (s && s.stale) winRows.push([h.host_id, `${s.service} / ${s.account}`, 'subscription', quotaBadge('stale'), 'unknown', 'unknown', 'unknown']);
    }
  }
  root.append(richTable(['Host', 'Provider', 'Window', 'State', 'Remaining', 'Native units', 'Reset'], winRows));
}
function richTable(columns, rows) {
  const wrapper = table(columns, []); const body = wrapper.querySelector('tbody'); if (rows.length) body.replaceChildren();
  for (const row of rows) { const tr = document.createElement('tr'); for (const [index, cell] of row.entries()) { const td = document.createElement('td'); td.dataset.label = columns[index]; if (cell instanceof Node) td.append(cell); else td.textContent = cell ?? 'unknown'; tr.append(td); } body.append(tr); }
  return wrapper;
}
const ago = s => s === null || s === undefined ? 'never' : s < 120 ? `${s}s ago` : s < 7200 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
function renderWatch(live, checks) {
  const open = live.alerts.filter(a => !a.resolved_at);
  byId('alerts').replaceChildren(open.length ? richTable(['Subject', 'State', 'Detail', 'Opened', 'Notified'], open.map(a => [`${a.subject_kind} ${a.subject}`, stateCell(a.state), a.detail, a.opened_at, a.notify_error ? `no: ${a.notify_error}` : (a.notified_at || 'pending')])) : Object.assign(document.createElement('p'), { textContent: 'No open alerts.' }));
  const order = { machine: 0, agent: 1, session: 2, launch: 3 };
  const subjects = [...live.subjects].sort((a, b) => order[a.kind] - order[b.kind] || a.id.localeCompare(b.id));
  byId('liveness').replaceChildren(richTable(['Kind', 'Subject', 'State', 'Last heartbeat / activity', 'Detail'], subjects.map(s => [s.kind, s.id, stateCell(s.state), ago(s.age_s), s.detail])));
  const runButton = name => { const b = document.createElement('button'); b.type = 'button'; b.className = 'dk-button dk-button--secondary dk-button--sm'; b.textContent = 'Run now'; b.setAttribute('aria-label', 'Run ' + name + ' now'); b.addEventListener('click', async () => { b.disabled = true; try { await api(`/api/checks/${encodeURIComponent(name)}/run`, 'POST', {}); await refresh(); } catch (e) { byId('checks-result').textContent = e.message; } finally { b.disabled = false; } }); return b; };
  byId('checks').replaceChildren(richTable(['Check', 'Kind', 'Every', 'Action', 'Last run', 'Result', ''], checks.checks.map(c => [c.name + (c.enabled ? '' : ' (disabled)'), c.kind, `${Math.round(c.interval_s / 60)} min`, `${c.action.type}${c.action.on === 'always' ? ' (always)' : ' (on failure)'}${c.action.to ? ' → ' + c.action.to : ''}`, c.last_run_at || 'never', c.last_status ? stateCell(c.last_status) : 'not run', runButton(c.name)])));
  const last = checks.runs.slice(0, 10);
  if (last.length) byId('checks').append(richTable(['Recent run', 'Check', 'Result', 'Detail', 'Action'], last.map(r => [r.at, r.name, stateCell(r.status), r.detail, r.action || ''])));
}
function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let b = bytes, i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${i === 0 ? b : b.toFixed(1)} ${units[i]}`;
}
function diskBadge(status) {
  const span = document.createElement('span');
  span.className = 'dk-badge ' + (status === 'critical' ? 'dk-badge--danger' : status === 'warn' ? 'dk-badge--warning' : 'dk-badge--success');
  span.textContent = status || 'unknown';
  return span;
}
function renderDisk(disk) {
  const root = byId('disk-monitoring');
  if (!root) return;
  root.replaceChildren();
  if (!disk || disk.error) {
    const p = document.createElement('p');
    p.textContent = disk && disk.error ? `Disk monitoring unavailable: ${disk.error}` : 'No disk monitoring reports yet.';
    root.append(p);
    return;
  }
  const machines = Array.isArray(disk.machines) ? disk.machines : [];
  if (!machines.length) {
    const p = document.createElement('p');
    p.textContent = 'No filesystem reports collected yet. Enable disk-monitor on reporting hosts.';
    root.append(p);
    return;
  }
  const rows = [];
  for (const m of machines) {
    for (const fs of m.filesystems || []) {
      let trendText = 'unknown';
      if (fs.trend && fs.trend.growth_bytes_per_day !== null && fs.trend.growth_bytes_per_day !== undefined) {
        const sign = fs.trend.growth_bytes_per_day >= 0 ? '+' : '';
        trendText = `${sign}${formatBytes(fs.trend.growth_bytes_per_day)}/day (${sign}${fs.trend.growth_pct_per_day ?? 0}%/day)`;
      } else if (fs.trend && fs.trend.state) {
        trendText = fs.trend.state;
      }
      let consumersNode = '-';
      if (Array.isArray(fs.top_consumers) && fs.top_consumers.length) {
        const div = document.createElement('div');
        div.className = 'disk-consumers';
        for (const c of fs.top_consumers) {
          const item = document.createElement('div');
          item.textContent = `${c.path} (${formatBytes(c.size_bytes)})`;
          div.append(item);
        }
        consumersNode = div;
      }
      const primaryInfo = fs.is_primary ? '' : ` (alias of ${fs.primary_mount || '/'})`;
      rows.push([
        m.machine_id,
        `${fs.mount_point}${primaryInfo}`,
        diskBadge(fs.status),
        `${fs.percent}%`,
        `${formatBytes(fs.used_bytes)} / ${formatBytes(fs.total_bytes)}`,
        formatBytes(fs.free_bytes),
        trendText,
        consumersNode
      ]);
    }
  }
  root.append(richTable(['Machine', 'Mount', 'Status', 'Usage', 'Used / Total', 'Free', 'Daily Trend', 'Top Consumers'], rows));
}
function healthBadge(status) {
  const span = document.createElement('span');
  span.className = 'dk-badge ' + (status === 'critical' ? 'dk-badge--danger' : status === 'warn' ? 'dk-badge--warning' : status === 'fresh' ? 'dk-badge--success' : 'dk-badge--warning');
  span.textContent = status || 'unknown';
  return span;
}
function renderHostHealth(health) {
  const root = byId('host-health');
  if (!root) return;
  root.replaceChildren();
  if (!health || health.error) {
    const p = document.createElement('p');
    p.textContent = health && health.error ? `Host health unavailable: ${health.error}` : 'No host health reports yet.';
    root.append(p);
    return;
  }
  const hosts = Array.isArray(health.machines) ? health.machines : [];
  if (!hosts.length) {
    const p = document.createElement('p');
    p.textContent = 'No host health reports collected yet. Enable host health monitoring on reporting hosts.';
    root.append(p);
    return;
  }
  const rows = [];
  for (const h of hosts) {
    const loadStr = h.load ? `${h.load.load1.toFixed(2)}, ${h.load.load5.toFixed(2)}, ${h.load.load15.toFixed(2)}` : 'unknown';
    const mem = h.memory || {};
    const memUsage = mem.percent !== undefined && mem.percent !== null ? `${mem.percent}%` : 'unknown';
    const memBytes = `${formatBytes(mem.used_bytes)} / ${formatBytes(mem.total_bytes)} (avail ${formatBytes(mem.available_bytes)})`;
    rows.push([
      h.machine_id,
      healthBadge(h.freshness),
      loadStr,
      healthBadge(mem.status),
      memUsage,
      memBytes,
      ago(Number.isFinite(Date.parse(h.observed_at)) ? Math.max(0, Math.round(age(h.observed_at) / 1000)) : null)
    ]);
  }
  root.append(richTable(['Machine', 'Freshness', 'Load (1m, 5m, 15m)', 'Memory Status', 'Memory Usage', 'Used / Total (Avail)', 'Observed'], rows));
}
async function refresh() {
  if (refreshing) return;
  refreshing = true; byId('fleet-refresh').disabled = true;
  try {
    const me = await api('/auth/me'); csrf = me.csrf_token;
    for (const id of ['account-name','menu-name','menu-email']) byId(id).textContent = me.email;
    document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0,1).toUpperCase();
    const [fleet, live, checks] = await Promise.all([api('/api/fleet'), api('/api/liveness'), api('/api/checks')]);
    render(fleet); renderWatch(live, checks);
    try {
      renderQuota(await api('/api/quota?view=fleet'));
    } catch (e) {
      renderQuota({ error: e.message });
    }
    try {
      renderDisk(await api('/api/monitoring/disk'));
    } catch (e) {
      renderDisk({ error: e.message });
    }
    try {
      renderHostHealth(await api('/api/monitoring/health'));
    } catch (e) {
      renderHostHealth({ error: e.message });
    }
    byId('status').textContent = `Authenticated · updated ${new Date().toLocaleTimeString()}`;
  }
  catch (e) { byId('status').textContent = 'Fleet could not be refreshed. Any displayed data is from the last successful update. ' + e.message; }
  finally { refreshing = false; byId('fleet-refresh').disabled = false; }
}
byId('launch').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const fields = new FormData(form);
  const input = Object.fromEntries([...fields].filter(([, value]) => value !== ''));
  if (input.mode === 'interactive' && (!input.machine || input.prompt)) { byId('launch-result').textContent = 'Choose a machine and leave the goal empty for an interactive session.'; return; }
  form.dataset.key ||= crypto.randomUUID(); input.idempotency_key = form.dataset.key;
  const button = form.querySelector('button'); button.disabled = true;
  try { const result = await api('/api/fleet/launches', 'POST', input); byId('launch-result').textContent = `${result.id}: ${result.state} on ${result.machine} (${result.engine}, ${result.account}).`; delete form.dataset.key; await refresh(); }
  catch (e) { byId('launch-result').textContent = `${e.message} Retry key retained: ${form.dataset.key}`; }
  finally { button.disabled = false; }
});
byId('registry').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const file = form.elements.registry.files[0];
  if (!file || file.size > 32768) { byId('registry-result').textContent = 'Choose a registry JSON under 32 KiB.'; return; }
  const button = form.querySelector('button'); button.disabled = true;
  try { await api('/api/fleet/accounts', 'PUT', JSON.parse(await file.text())); byId('registry-result').textContent = 'Account labels and routes saved.'; await refresh(); }
  catch (e) { byId('registry-result').textContent = e.message; }
  finally { button.disabled = false; }
});
byId('checks-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const file = form.elements.checks.files[0];
  if (!file || file.size > 65536) { byId('checks-result').textContent = 'Choose a checks JSON under 64 KiB.'; return; }
  const button = form.querySelector('button'); button.disabled = true;
  try { await api('/api/checks', 'PUT', JSON.parse(await file.text())); byId('checks-result').textContent = 'Checks saved.'; await refresh(); }
  catch (e) { byId('checks-result').textContent = e.message; }
  finally { button.disabled = false; }
});
byId('fleet-refresh').addEventListener('click', refresh);
byId('sign-out').addEventListener('click', async () => {
  byId('sign-out').disabled = true;
  try {
    const me = await api('/auth/me');
    const response = await fetch('/auth/logout', {method:'POST',credentials:'same-origin',redirect:'manual',headers:{'X-CSRF-Token':me.csrf_token}});
    if (response.type !== 'opaqueredirect' && !response.ok) throw Error('Sign-out failed');
    csrf = null; location.assign('/auth/login');
  } catch { byId('status').textContent = 'Sign-out could not be confirmed. Try again.'; byId('sign-out').disabled = false; }
});
refresh(); setInterval(refresh, 30000);
