/* Render server data as text; never inject HTML, transcripts or credentials. */
const byId = id => document.getElementById(id);
let csrf = null;
const age = at => {
  const value = Date.parse(at);
  return Number.isFinite(value) ? Date.now() - value : Infinity;
};
const fresh = at => age(at) >= 0 && age(at) <= 180000;
function table(columns, rows) {
  const wrapper = document.createElement('div'); wrapper.className = 'scroll';
  const t = document.createElement('table'); const head = document.createElement('tr');
  for (const title of columns) { const th = document.createElement('th'); th.textContent = title; head.append(th); }
  const thead = document.createElement('thead'); thead.append(head); t.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) { const tr = document.createElement('tr'); for (const text of row) { const td = document.createElement('td'); td.textContent = text ?? 'unknown'; tr.append(td); } body.append(tr); }
  t.append(body); wrapper.append(t); return wrapper;
}
async function api(path, method = 'GET', data) {
  const headers = {};
  if (data !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf; }
  const r = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', body: data === undefined ? undefined : JSON.stringify(data) });
  if (r.status === 401) { location.assign('/auth/login'); throw Error('Sign-in required'); }
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
  byId('sessions').replaceChildren(table(['Machine', 'Workspace', 'Tag', 'Engine / profile / account', 'Status', 'Report'], data.sessions.map(s => [s.machine_id, s.workspace, s.tag, `${s.engine || 'shell'} / ${s.profile || 'default'} / ${s.account || 'unknown'}`, `${s.phase} / ${s.agent_state || 'unknown'}`, fresh(s.observed_at) ? s.observed_at : `stale: ${s.observed_at}`])));
  byId('registered-agents').replaceChildren(table(['Agent', 'Project / role', 'Machine', 'Engine', 'Session', 'Health'], data.agents.map(a => [a.tag, `${a.project} / ${a.role}`, a.machine_id, a.engine, a.session_id, `${a.health_state || 'unknown'} / ${a.health_at || 'no report'}`])));
  const select = byId('machines'); const selected = select.value;
  select.replaceChildren(new Option('Choose automatically', ''));
  for (const m of data.machines) select.append(new Option(m.name || m.id, m.id));
  select.value = selected;
}
async function refresh() {
  try { const me = await api('/auth/me'); csrf = me.csrf_token; render(await api('/api/fleet')); byId('status').textContent = `Authenticated · updated ${new Date().toLocaleTimeString()}`; }
  catch (e) { byId('status').textContent = e.message; }
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
refresh(); setInterval(refresh, 30000);
