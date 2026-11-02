/* Prompt library + session schedules dashboard (intake 19ec91c2 AC8-AC10).
 * Prompt bodies are fetched only on demand and rendered as text; schedule and
 * delivery rows carry ids/statuses, never prompt bodies. */
const byId = id => document.getElementById(id);
let csrf = null;
let selectedSchedule = null;
let listScroll = 0;

async function api(path, method = 'GET', data) {
  const headers = {};
  if (method !== 'GET') {
    if (!csrf) csrf = (await (await fetch('/auth/me', { credentials: 'same-origin' })).json()).csrf_token || null;
    headers['X-CSRF-Token'] = csrf;
    if (data !== undefined) headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store',
    body: data === undefined ? undefined : JSON.stringify(data) });
  if (r.status === 401) { location.assign('/auth/login'); throw new Error('Sign-in required'); }
  if (!r.ok) {
    let text = `API request failed (${r.status})`;
    try { const body = await r.json(); if (body && body.error) text = `${body.error} (${r.status})`; } catch {}
    throw new Error(text);
  }
  return r.json();
}

const setStatus = text => { byId('ps-status').textContent = text || ''; };


function cadenceLabel(cron, timezone) {
  const readable = cron === '0 9 * * *' ? 'Daily at 09:00' : cron === '0 * * * *' ? 'Every hour' : `Cron: ${cron}`;
  return `${readable} · ${timezone || 'timezone unknown'}`;
}

function cell(row, text, mono = false) {
  const td = document.createElement('td');
  if (mono) td.className = 'dk-mono';
  td.textContent = text === undefined || text === null ? '' : String(text);
  row.appendChild(td);
  return td;
}

function button(label, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'dk-button dk-button--secondary dk-button--sm';
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

function empty(body, cols, text) {
  body.textContent = '';
  const tr = document.createElement('tr');
  cell(tr, text).colSpan = cols;
  body.appendChild(tr);
}

async function loadPrompts() {
  const body = byId('ps-prompts-body');
  const data = await api('/api/prompts');
  if (!data.prompts.length) return empty(body, 5, 'No prompts yet (hub prompts create)');
  body.textContent = '';
  for (const p of data.prompts) {
    const tr = document.createElement('tr');
    cell(tr, p.title); cell(tr, p.slug, true); cell(tr, 'v' + p.current_version); cell(tr, p.updated_at);
    const actions = document.createElement('td');
    actions.appendChild(button('View', async () => {
      const pre = byId('ps-prompt-body');
      pre.hidden = false; pre.textContent = 'Loading prompt…';
      try {
        const detail = await api('/api/prompts/' + encodeURIComponent(p.slug));
        pre.textContent = detail.body;
      } catch (err) { pre.textContent = 'Prompt unavailable: ' + err.message; }
    }));
    tr.appendChild(actions);
    body.appendChild(tr);
  }
}

async function act(label, fn) {
  try { await fn(); setStatus(label + ' done.'); await loadAll(); } catch (err) { setStatus(`${label} failed: ${err.message}`); }
}

async function loadSchedules() {
  const body = byId('ps-schedules-body');
  const data = await api('/api/session-schedules');
  if (!data.schedules.length) return empty(body, 6, 'No send-prompt schedules yet.');
  body.textContent = '';
  for (const s of data.schedules) {
    const tr = document.createElement('tr');
    cell(tr, s.name); cell(tr, s.target_label); cell(tr, cadenceLabel(s.cron, s.tz));
    cell(tr, s.next_due_at || 'Unknown'); cell(tr, s.enabled ? 'Enabled' : 'Paused');
    const actions = document.createElement('td');
    const ref = encodeURIComponent(s.name);
    actions.appendChild(button('Open', () => loadDeliveries(s)));
    actions.appendChild(button(s.enabled ? 'Pause' : 'Resume', () => act(s.enabled ? 'Pause' : 'Resume',
      () => api(`/api/session-schedules/${ref}/${s.enabled ? 'pause' : 'resume'}`, 'POST', {}))));
    actions.appendChild(button('Send now', () => { if (confirm('Send this prompt to the existing target session now?')) act('Send', () => api(`/api/session-schedules/${ref}/fire`, 'POST', {})); }));
    tr.appendChild(actions);
    body.appendChild(tr);
  }
}

async function loadDeliveries(schedule) {
  listScroll = window.scrollY;
  document.querySelector('main').dataset.view = 'detail';
  selectedSchedule = schedule.name;
  const body = byId('ps-deliveries-body');
  byId('ps-schedule-detail').hidden = false;
  if (matchMedia('(max-width:700px)').matches) byId('ps-schedule-detail').scrollIntoView({ block: 'start' });
  byId('ps-detail-summary').textContent = `${schedule.name} → ${schedule.target_label} · Loading history…`;
  empty(body, 8, 'Loading deliveries…');
  try {
    const data = await api('/api/session-schedules/' + encodeURIComponent(schedule.name) + '/deliveries');
    if (selectedSchedule !== schedule.name) return;
    byId('ps-detail-summary').textContent = `${schedule.name} → ${schedule.target_label} · ${schedule.cron} · ${schedule.tz}`;
    if (!data.deliveries.length) return empty(body, 8, 'No deliveries for this schedule yet.');
    body.textContent = '';
    for (const d of data.deliveries) {
      const tr = document.createElement('tr');
      cell(tr, d.id, true); cell(tr, schedule.name); cell(tr, d.target_label); cell(tr, d.status);
      cell(tr, d.attempts); cell(tr, d.last_error); cell(tr, d.created_at); cell(tr, d.delivered_at);
      body.appendChild(tr);
    }
  } catch (err) {
    if (selectedSchedule !== schedule.name) return;
    empty(body, 8, 'Delivery history unavailable: ' + err.message);
  }
}

async function loadAll() {
  const requests = [[loadPrompts, 'ps-prompts-body', 5], [loadSchedules, 'ps-schedules-body', 6]];
  await Promise.all(requests.map(async ([load, id, columns]) => {
    try { await load(); } catch (err) { empty(byId(id), columns, 'Load failed: ' + err.message); setStatus('Load failed: ' + err.message); }
  }));
}

function promptRoute() {
  const library = location.hash === '#library';
  byId('library').hidden = !library;
  byId('ps-schedules-panel').hidden = library;
  byId('ps-schedule-detail').hidden = true;
  selectedSchedule = null;
  document.querySelector('main').dataset.view = 'list';
}

async function init() {
  const signOut = byId('sign-out');
  if (signOut) signOut.addEventListener('click', async () => {
    if (!csrf) csrf = (await (await fetch('/auth/me', { credentials: 'same-origin' })).json()).csrf_token || null;
    await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': csrf } });
    location.assign('/');
  });
  byId('ps-close-detail').addEventListener('click', () => { selectedSchedule = null; byId('ps-schedule-detail').hidden = true; document.querySelector('main').dataset.view = 'list'; window.scrollTo(0, listScroll); });
  addEventListener('hashchange', promptRoute);
  promptRoute();
  await loadAll();
}

init();

// Preserve column context when tables become one-column cards on phones.
function labelSecondaryTables() {
  document.querySelectorAll('main table').forEach(table => {
    const labels = [...table.querySelectorAll('thead th')].map(th => th.textContent);
    table.querySelectorAll('tbody tr').forEach(row => [...row.children].forEach((cell, index) => {
      if (cell.colSpan === 1) cell.dataset.label = labels[index] || '';
    }));
  });
}
new MutationObserver(labelSecondaryTables).observe(document.querySelector('main'), { childList: true, subtree: true });
labelSecondaryTables();
