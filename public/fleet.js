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
async function refresh() {
  if (refreshing) return;
  refreshing = true; byId('fleet-refresh').disabled = true;
  try {
    const me = await api('/auth/me'); csrf = me.csrf_token;
    for (const id of ['account-name','menu-name','menu-email']) byId(id).textContent = me.email;
    document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0,1).toUpperCase();
    const [fleet, live, checks] = await Promise.all([api('/api/fleet'), api('/api/liveness'), api('/api/checks')]);
    render(fleet); renderWatch(live, checks);
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
