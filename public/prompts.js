/* Prompt library + session schedules dashboard (intake 19ec91c2 AC8-AC10).
 * Prompt bodies are fetched only on demand and rendered as text; schedule and
 * delivery rows carry ids/statuses, never prompt bodies. */
const byId = id => document.getElementById(id);
let csrf = null;

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
    cell(tr, p.slug, true); cell(tr, p.title); cell(tr, 'v' + p.current_version); cell(tr, p.updated_at);
    const actions = document.createElement('td');
    actions.appendChild(button('View', async () => {
      const detail = await api('/api/prompts/' + encodeURIComponent(p.slug));
      const pre = byId('ps-prompt-body');
      pre.textContent = detail.body;
      pre.hidden = false;
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
  if (!data.schedules.length) return empty(body, 8, 'No session schedules yet (hub schedules create)');
  body.textContent = '';
  for (const s of data.schedules) {
    const tr = document.createElement('tr');
    cell(tr, s.name, true); cell(tr, s.target_label); cell(tr, `${s.cron} (${s.tz})`, true);
    cell(tr, s.prompt_id ? 'library: ' + s.prompt_id : `inline (${s.prompt_bytes} bytes)`);
    cell(tr, s.enabled ? 'yes' : 'paused'); cell(tr, s.next_due_at); cell(tr, s.last_fire_at);
    const actions = document.createElement('td');
    const ref = encodeURIComponent(s.name);
    actions.appendChild(button(s.enabled ? 'Pause' : 'Resume', () => act(s.enabled ? 'Pause' : 'Resume',
      () => api(`/api/session-schedules/${ref}/${s.enabled ? 'pause' : 'resume'}`, 'POST', {}))));
    actions.appendChild(button('Fire now', () => act('Fire', () => api(`/api/session-schedules/${ref}/fire`, 'POST', {}))));
    tr.appendChild(actions);
    body.appendChild(tr);
  }
}

async function loadDeliveries() {
  const body = byId('ps-deliveries-body');
  const data = await api('/api/session-schedules/deliveries');
  body.textContent = '';
  if (!data.deliveries.length) return empty(body, 8, 'No schedule deliveries yet');
  for (const d of data.deliveries) {
    const tr = document.createElement('tr');
    cell(tr, d.id, true); cell(tr, d.schedule); cell(tr, d.target_label); cell(tr, d.status);
    cell(tr, d.attempts); cell(tr, d.last_error); cell(tr, d.created_at); cell(tr, d.delivered_at);
    body.appendChild(tr);
  }
}

async function loadAll() {
  try { await Promise.all([loadPrompts(), loadSchedules(), loadDeliveries()]); }
  catch (err) { setStatus('Load failed: ' + err.message); }
}

async function init() {
  const signOut = byId('sign-out');
  if (signOut) signOut.addEventListener('click', async () => {
    if (!csrf) csrf = (await (await fetch('/auth/me', { credentials: 'same-origin' })).json()).csrf_token || null;
    await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': csrf } });
    location.assign('/');
  });
  await loadAll();
}

init();
