/* Launch schedules dashboard client (intake a839dfb6). Every create/pause/
 * resume/delete/tick goes through the admin API; runs are read-only history. */
const byId = id => document.getElementById(id);
let csrf = null;
let selectedSchedule = null;
let listScroll = 0;

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
      if (body && body.error) text = `${body.error}${body.detail ? `: ${body.detail}` : ''} (${r.status})`;
    } catch {}
    throw new Error(text);
  }
  return r.json();
}

function setStatus(text) {
  const el = byId('sc-status');
  if (el) el.textContent = text || '';
}


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

function outcomeChip(outcome) {
  const chip = document.createElement('span');
  chip.className = 'dk-badge ' + ({
    started: 'dk-badge--success', held: 'dk-badge--info', pending: 'dk-badge--warn',
    skipped: 'dk-badge--warn', refused: 'dk-badge--danger', error: 'dk-badge--danger',
  }[outcome] || '');
  chip.textContent = outcome || 'unknown';
  return chip;
}

function actionButton(label, handler) {
  const button = document.createElement('button');
  button.className = 'dk-button dk-button--secondary dk-button--sm';
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

async function loadSchedules() {
  const body = byId('sc-body');
  try {
    const data = await api('/api/schedules');
    body.textContent = '';
    if (!data.schedules.length) {
      const tr = document.createElement('tr');
      cell(tr, 'No launch schedules yet', false).colSpan = 6;
      body.appendChild(tr);
      return;
    }
    for (const schedule of data.schedules) {
      const tr = document.createElement('tr');
      cell(tr, schedule.name);
      cell(tr, schedule.project, true);
      cell(tr, cadenceLabel(schedule.cron, schedule.tz));
      cell(tr, schedule.next_due_at || (schedule.enabled ? '—' : 'paused'));
      cell(tr, schedule.enabled ? 'Enabled' : 'Paused');
      const actions = document.createElement('td');
      actions.appendChild(actionButton('Open', () => openSchedule(schedule.id)));
      actions.appendChild(document.createTextNode(' '));
      actions.appendChild(schedule.enabled
        ? actionButton('Pause', () => setEnabled(schedule, false))
        : actionButton('Resume', () => setEnabled(schedule, true)));
      actions.appendChild(document.createTextNode(' '));
      actions.appendChild(actionButton('Delete', () => deleteSchedule(schedule)));
      tr.appendChild(actions);
      body.appendChild(tr);
    }
  } catch (err) {
    body.textContent = '';
    const tr = document.createElement('tr');
    cell(tr, `Load failed: ${err.message}`).colSpan = 6;
    body.appendChild(tr);
  }
}

async function openSchedule(id) {
  if (document.querySelector('main').dataset.view !== 'detail') listScroll = window.scrollY;
  document.querySelector('main').dataset.view = 'detail';
  selectedSchedule = id;
  byId('sc-detail').hidden = false;
  if (matchMedia('(max-width:700px)').matches) byId('sc-detail').scrollIntoView({ block: 'start' });
  byId('sc-detail-name').textContent = '';
  byId('sc-runs-body').replaceChildren();
  byId('sc-detail-summary').textContent = 'Loading schedule…';
  setStatus('Loading runs…');
  try {
    const [detail, runs] = await Promise.all([
      api(`/api/schedules/${encodeURIComponent(id)}`),
      api(`/api/schedules/${encodeURIComponent(id)}/runs?limit=50`),
    ]);
    if (selectedSchedule !== id) return;
    byId('sc-detail-name').textContent = detail.name;
    byId('sc-detail-summary').textContent = `${detail.project || 'Unknown project'} · ${detail.cron} · ${detail.tz} · ${detail.enabled ? 'Enabled' : 'Paused'}`;
    const body = byId('sc-runs-body');
    body.textContent = '';
    for (const run of runs.runs || detail.runs || []) {
      const tr = document.createElement('tr');
      cell(tr, run.tick_at, true);
      cell(tr, run.fired_at, true);
      const outcome = document.createElement('td');
      outcome.appendChild(outcomeChip(run.outcome));
      tr.appendChild(outcome);
      cell(tr, run.intake_id, true);
      cell(tr, run.session_id, true);
      cell(tr, run.reason || '');
      body.appendChild(tr);
    }
    if (!(runs.runs || detail.runs || []).length) {
      const tr = document.createElement('tr');
      cell(tr, 'No runs yet').colSpan = 6;
      body.appendChild(tr);
    }
    setStatus('');
  } catch (err) {
    if (selectedSchedule !== id) return;
    byId('sc-detail-summary').textContent = `Detail failed: ${err.message}`;
    setStatus(`Detail failed: ${err.message}`);
  }
}

async function setEnabled(schedule, enabled) {
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    await api(`/api/schedules/${encodeURIComponent(schedule.id)}/${enabled ? 'resume' : 'pause'}`, 'POST', {});
    setStatus(`${enabled ? 'Resumed' : 'Paused'} ${schedule.name}.`);
    await loadSchedules();
  } catch (err) {
    setStatus(`${enabled ? 'Resume' : 'Pause'} failed: ${err.message}`);
  }
}

async function deleteSchedule(schedule) {
  if (!confirm(`Delete launch schedule "${schedule.name}"? Its run history is kept.`)) return;
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    await api(`/api/schedules/${encodeURIComponent(schedule.id)}`, 'DELETE', {});
    setStatus(`Deleted ${schedule.name}.`);
    if (selectedSchedule === schedule.id) {
      byId('sc-detail').hidden = true;
      selectedSchedule = null;
    }
    await loadSchedules();
  } catch (err) {
    setStatus(`Delete failed: ${err.message}`);
  }
}

async function createSchedule(event) {
  event.preventDefault();
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    const payload = {
      name: byId('sc-name').value.trim(),
      project: byId('sc-project').value.trim(),
      cron: byId('sc-cron').value.trim(),
      tz: byId('sc-tz').value.trim() || 'UTC',
      task_kind: byId('sc-task-kind').value,
      overlap: byId('sc-overlap').value,
      prompt: byId('sc-prompt').value,
    };
    const engine = byId('sc-engine').value.trim();
    if (engine) payload.engine = engine;
    const tier = byId('sc-tier').value.trim();
    if (tier) payload.tier = tier;
    const acceptance = byId('sc-ac').value;
    if (acceptance.trim()) payload.acceptance_criteria = acceptance;
    const result = await api('/api/schedules', 'POST', payload);
    setStatus(result.acceptance_criteria_warning
      ? `Created ${result.name}. Warning: ${result.acceptance_criteria_warning}`
      : `Created ${result.name}. Next due ${result.next_due_at || 'when resumed'}.`);
    byId('sc-create-form').reset();
    await loadSchedules();
  } catch (err) {
    setStatus(`Create failed: ${err.message}`);
  }
}

async function tickSchedule() {
  if (!selectedSchedule) return;
  if (!confirm('Run this due tick now? It can start a new session.')) return;
  try {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    const result = await api(`/api/schedules/${encodeURIComponent(selectedSchedule)}/tick`, 'POST', {});
    setStatus(`Tick ${result.state || result.outcome || 'done'}${result.reason ? `: ${result.reason}` : ''}.`);
    await Promise.all([loadSchedules(), openSchedule(selectedSchedule)]);
  } catch (err) {
    setStatus(`Tick failed: ${err.message}`);
  }
}

async function init() {
  const signOut = byId('sign-out');
  if (signOut) signOut.addEventListener('click', async () => {
    if (!csrf) csrf = (await api('/auth/me')).csrf_token || null;
    await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': csrf } });
    location.assign('/');
  });
  byId('sc-close-detail').addEventListener('click', () => { selectedSchedule = null; byId('sc-detail').hidden = true; document.querySelector('main').dataset.view = 'list'; window.scrollTo(0, listScroll); });
  byId('sc-cadence').addEventListener('change', event => {
    const custom = event.target.value === 'custom';
    byId('sc-cron-panel').open = custom;
    if (!custom) byId('sc-cron').value = event.target.value;
    else byId('sc-cron').focus();
  });
  byId('sc-create-form').addEventListener('submit', createSchedule);
  byId('sc-tick').addEventListener('click', tickSchedule);
  await loadSchedules();
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
