/* Webhooks dashboard client (intake 19ec91c2). Secrets are never rendered:
 * the create response is shown once in the creation panel and then dropped
 * from memory; list/show payloads contain no secret material by design. */
const byId = id => document.getElementById(id);
let csrf = null;
let selectedRoute = null;

async function api(path, method = 'GET', data) {
  const headers = {};
  if (data !== undefined) {
    headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  const r = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store',
    body: data === undefined ? undefined : JSON.stringify(data) });
  if (r.status === 401) {
    location.assign('/auth/login');
    throw new Error('Sign-in required');
  }
  if (!r.ok) {
    let text = `API request failed (${r.status})`;
    try {
      const body = await r.json();
      if (body && body.error) text = `${body.error} (${r.status})`;
    } catch {}
    throw new Error(text);
  }
  return r.json();
}

function setStatus(text) {
  const el = byId('wh-status');
  if (el) el.textContent = text || '';
}

function cell(row, text, mono = false) {
  const td = document.createElement('td');
  if (mono) td.className = 'dk-mono';
  td.textContent = text === undefined || text === null ? '' : String(text);
  row.appendChild(td);
  return td;
}

function statusChip(status) {
  const chip = document.createElement('span');
  chip.className = 'dk-badge ' + ({
    delivered: 'dk-badge--success', queued: 'dk-badge--info', pending: 'dk-badge--warn', failed: 'dk-badge--danger',
  }[status] || '');
  chip.textContent = status || 'unknown';
  return chip;
}

async function loadRoutes() {
  const body = byId('wh-routes-body');
  try {
    const data = await api('/api/webhooks');
    body.textContent = '';
    if (!data.webhooks.length) {
      const tr = document.createElement('tr');
      cell(tr, 'No webhook routes yet', false).colSpan = 7;
      body.appendChild(tr);
      return;
    }
    for (const route of data.webhooks) {
      const tr = document.createElement('tr');
      cell(tr, route.name);
      cell(tr, route.target_label, true);
      cell(tr, route.auth_mode);
      cell(tr, route.rate_limit_per_min);
      cell(tr, route.max_body_bytes);
      cell(tr, route.enabled ? 'yes' : 'no');
      const actions = document.createElement('td');
      const open = document.createElement('button');
      open.className = 'dk-button dk-button--secondary dk-button--sm';
      open.type = 'button';
      open.textContent = 'Open';
      open.addEventListener('click', () => openRoute(route.name));
      const del = document.createElement('button');
      del.className = 'dk-button dk-button--secondary dk-button--sm';
      del.type = 'button';
      del.textContent = 'Delete';
      del.addEventListener('click', () => deleteRoute(route));
      actions.appendChild(open);
      actions.appendChild(document.createTextNode(' '));
      actions.appendChild(del);
      tr.appendChild(actions);
      body.appendChild(tr);
    }
  } catch (err) {
    body.textContent = '';
    const tr = document.createElement('tr');
    cell(tr, `Load failed: ${err.message}`).colSpan = 7;
    body.appendChild(tr);
  }
}

async function openRoute(name) {
  selectedRoute = name;
  byId('wh-detail').hidden = false;
  byId('wh-detail-name').textContent = name;
  setStatus('Loading deliveries…');
  try {
    const detail = await api(`/api/webhooks/${encodeURIComponent(name)}`);
    byId('wh-ingest-url').textContent = `${location.origin}${detail.url_path}`;
    const body = byId('wh-deliveries-body');
    body.textContent = '';
    for (const delivery of detail.deliveries || []) {
      const tr = document.createElement('tr');
      cell(tr, delivery.id, true);
      cell(tr, delivery.source);
      const status = document.createElement('td');
      status.appendChild(statusChip(delivery.status));
      tr.appendChild(status);
      cell(tr, delivery.attempts);
      cell(tr, delivery.session_id, true);
      cell(tr, delivery.last_error || '');
      cell(tr, delivery.created_at);
      cell(tr, delivery.delivered_at || '');
      body.appendChild(tr);
    }
    if (!(detail.deliveries || []).length) {
      const tr = document.createElement('tr');
      cell(tr, 'No deliveries yet').colSpan = 8;
      body.appendChild(tr);
    }
    const audit = byId('wh-audit-list');
    audit.textContent = '';
    for (const entry of detail.audit || []) {
      const li = document.createElement('li');
      let detailText = '';
      try { detailText = JSON.stringify(JSON.parse(entry.detail || '{}')); } catch {}
      li.textContent = `${entry.at} ${entry.action} (${entry.actor}) ${detailText}`;
      li.className = 'dk-mono';
      audit.appendChild(li);
    }
    setStatus('');
  } catch (err) {
    setStatus(`Detail failed: ${err.message}`);
  }
}

async function deleteRoute(route) {
  if (!confirm(`Delete webhook route "${route.name}"? In-flight deliveries fail with route_deleted.`)) return;
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    await api(`/api/webhooks/${encodeURIComponent(route.id)}`, 'DELETE', {});
    setStatus(`Deleted ${route.name}.`);
    if (selectedRoute === route.name) {
      byId('wh-detail').hidden = true;
      selectedRoute = null;
    }
    await loadRoutes();
  } catch (err) {
    setStatus(`Delete failed: ${err.message}`);
  }
}

async function createRoute(event) {
  event.preventDefault();
  const created = byId('wh-created');
  created.hidden = true;
  created.textContent = '';
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    const payload = {
      name: byId('wh-name').value.trim(),
      target_label: byId('wh-target').value.trim(),
      auth_mode: byId('wh-auth').value,
      rate_limit_per_min: Number(byId('wh-rate').value) || undefined,
      max_body_bytes: Number(byId('wh-max-body').value) || undefined,
    };
    const template = byId('wh-template').value.trim();
    if (template) payload.template = template;
    const result = await api('/api/webhooks', 'POST', payload);
    const heading = document.createElement('p');
    heading.textContent = `Route created. Ingest URL: ${location.origin}${result.url_path}`;
    created.appendChild(heading);
    const secretNote = document.createElement('p');
    secretNote.textContent = 'Secret (shown once, copy it now — never logs/reports):';
    created.appendChild(secretNote);
    const secretCode = document.createElement('code');
    secretCode.className = 'dk-mono';
    secretCode.textContent = result.secret || '(not returned)';
    created.appendChild(secretCode);
    created.hidden = false;
    byId('wh-create-form').reset();
    await loadRoutes();
  } catch (err) {
    setStatus(`Create failed: ${err.message}`);
  }
}

async function sendTest() {
  if (!selectedRoute) return;
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    const result = await api(`/api/webhooks/${encodeURIComponent(selectedRoute)}/test`, 'POST', {});
    setStatus(`Test delivery ${result.delivery_id} is ${result.status}.`);
    await openRoute(selectedRoute);
  } catch (err) {
    setStatus(`Test failed: ${err.message}`);
  }
}

async function init() {
  const signOut = byId('sign-out');
  if (signOut) signOut.addEventListener('click', async () => {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': csrf } });
    location.assign('/');
  });
  byId('wh-create-form').addEventListener('submit', createRoute);
  byId('wh-test').addEventListener('click', sendTest);
  await loadRoutes();
}

init();
