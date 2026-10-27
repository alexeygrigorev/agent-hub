/* Intake dashboard client: list with filter, read-only detail open, and the
   three operator actions (note, status, approve). Opening a task only ever
   issues GET requests: this file never calls assign, continue, session start,
   or fleet launches. Delivery states are rendered with their real names;
   nothing here maps a delivery to "accepted". Worktree refresh/remove and
   Close intake (with the worktree close gate) are explicit button POSTs. */
const byId = id => document.getElementById(id);
let cachedTasks = [];
let selectedTaskId = null;
let detailVersion = null;
let pendingApprovalRef = null;
let csrf = null;
let loading = false;
let cachedDetail = null;

async function api(path, method = 'GET', data) {
  const headers = {};
  if (data !== undefined) {
    headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  const r = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  if (r.status === 401) {
    csrf = null;
    location.assign('/auth/login');
    throw new Error('Sign-in required');
  }
  if (!r.ok) {
    let errText = `API request failed (${r.status})`;
    let errBody = null;
    try {
      errBody = await r.json();
      if (errBody && errBody.error) errText = `${errBody.error} (${r.status})`;
    } catch {}
    const err = new Error(errText);
    err.status = r.status;
    err.body = errBody && typeof errBody === 'object' ? errBody : null;
    throw err;
  }
  return r.json();
}

function reqKey(prefix) {
  const rand = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
    ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 20)
    : `${Date.now()}${Math.floor(Math.random() * 1e6)}`;
  return `${prefix}-${rand}`;
}

function statusBadge(status) {
  const span = document.createElement('span');
  const s = status || 'unknown';
  let color = 'dk-badge--neutral';
  if (['done'].includes(s)) color = 'dk-badge--success';
  else if (['open', 'in_progress', 'pending_approval', 'blocked'].includes(s)) color = 'dk-badge--warning';
  else if (['cancelled'].includes(s)) color = 'dk-badge--danger';
  span.className = `dk-badge ${color}`;
  span.textContent = s;
  return span;
}

function taskPath(id) {
  return `/intake/${encodeURIComponent(id)}`;
}

function taskIdFromPath() {
  const m = /^\/intake\/([A-Za-z0-9._:-]{1,128})$/.exec(location.pathname);
  return m ? decodeURIComponent(m[1]) : null;
}

function taskTitle(t) {
  return (t.payload_data && t.payload_data.title) || t.id;
}

function bindTaskTap(el, taskId, labelText) {
  if (!taskId) return;
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.dataset.taskId = taskId;
  el.setAttribute('aria-label', `Open task ${labelText || taskId}`);
  el.addEventListener('click', e => {
    e.stopPropagation();
    openTaskDetail(taskId);
  });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      openTaskDetail(taskId);
    }
  });
}

function renderList(tasks, filterText) {
  const container = byId('intake-list');
  container.replaceChildren();
  const q = (filterText || '').trim().toLowerCase();
  const filtered = !q ? tasks : tasks.filter(t => {
    const hay = `${taskTitle(t)} ${(t.project || '')} ${((t.control && t.control.status) || '')}`.toLowerCase();
    return hay.includes(q);
  });
  const statusEl = byId('intake-status');
  if (statusEl) statusEl.textContent = `Authenticated · ${filtered.length} task${filtered.length === 1 ? '' : 's'}`;
  if (!filtered.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = 'No tasks match this filter.';
    container.append(p);
    return;
  }
  const list = document.createElement('div');
  list.className = 'intake-list';
  for (const t of filtered) {
    const item = document.createElement('div');
    item.className = 'intake-item';
    const main = document.createElement('div');
    main.className = 'intake-item__main';
    const title = document.createElement('span');
    title.className = 'intake-item__title';
    title.textContent = taskTitle(t);
    title.title = taskTitle(t);
    const sub = document.createElement('span');
    sub.className = 'intake-item__sub';
    const cont = t.control && t.control.continuation_state ? ` · ${t.control.continuation_state}` : '';
    sub.textContent = `${t.project || '-'} · ${t.id}${cont}`;
    sub.title = sub.textContent;
    main.append(title, sub);
    const badges = document.createElement('div');
    badges.className = 'intake-item__badges';
    badges.append(statusBadge(t.control && t.control.status));
    const sessions = Array.isArray(t.assigned_sessions) ? t.assigned_sessions.length : 0;
    if (sessions) {
      const s = document.createElement('span');
      s.className = 'dk-badge dk-badge--info dk-mono';
      s.textContent = `${sessions} session${sessions === 1 ? '' : 's'}`;
      badges.append(s);
    }
    item.append(main, badges);
    bindTaskTap(item, t.id, taskTitle(t));
    list.append(item);
  }
  container.append(list);
}

function addMetaItem(container, label, value, onClick) {
  const item = document.createElement('div');
  item.className = 'session-meta-item';
  const lbl = document.createElement('span');
  lbl.className = 'session-meta-item__label';
  lbl.textContent = label;
  item.append(lbl);
  if (typeof onClick === 'function') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'session-meta-link';
    btn.textContent = value || '-';
    btn.addEventListener('click', onClick);
    item.append(btn);
  } else {
    const val = document.createElement('span');
    val.className = 'session-meta-item__value';
    val.textContent = value || '-';
    item.append(val);
  }
  container.append(item);
}

function historyText(e) {
  if (typeof e.text === 'string' && e.text) return e.text;
  if (typeof e.reference === 'string' && e.reference) return e.reference;
  if (typeof e.outcome_ref === 'string' && e.outcome_ref) return e.outcome_ref;
  if (e.changes && typeof e.changes === 'object') return Object.keys(e.changes).join(', ');
  return '';
}

/* Worktrees (intake 4b161918). Host status is shown exactly as reported:
   a null/missing field renders "unknown", never a guessed yes/no. */
function fmtTime(value) {
  if (value === null || value === undefined || value === '') return '-';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toISOString().slice(0, 19).replace('T', ' ');
}

function triState(value) {
  if (value === null || value === undefined) return 'unknown';
  if (typeof value === 'number') return value > 0 ? 'yes' : 'no';
  return value ? 'yes' : 'no';
}

/* tone: which answer is fine for closing. */
function statusChip(label, value, goodWhen, count) {
  const chip = document.createElement('span');
  const answer = triState(value);
  let tone = 'dk-badge--neutral';
  if (answer !== 'unknown') tone = answer === goodWhen ? 'dk-badge--success' : 'dk-badge--warning';
  chip.className = `dk-badge dk-mono intake-wt-chip ${tone}`;
  const n = typeof count === 'number' && answer === 'yes' ? ` (${count})` : '';
  chip.textContent = `${label}: ${answer}${n}`;
  chip.dataset.state = answer;
  return chip;
}

function wtLine(label, value) {
  const line = document.createElement('div');
  line.className = 'intake-wt-line';
  const lbl = document.createElement('span');
  lbl.className = 'session-meta-item__label';
  lbl.textContent = label;
  const val = document.createElement('span');
  val.className = 'session-meta-item__value';
  val.textContent = value === null || value === undefined || value === '' ? '-' : String(value);
  line.append(lbl, val);
  return line;
}

function pendingCommands(w) {
  const list = Array.isArray(w.pending) ? w.pending : (Array.isArray(w.pending_commands) ? w.pending_commands : []);
  return list.filter(c => c && !['done', 'failed'].includes(c.state));
}

function renderCloseGate(task) {
  const el = byId('intake-close-gate');
  if (!el) return;
  el.replaceChildren();
  const gate = task.close_gate;
  if (!gate || typeof gate !== 'object') { el.hidden = true; return; }
  el.hidden = false;
  const badge = document.createElement('span');
  badge.className = `dk-badge ${gate.ok ? 'dk-badge--success' : 'dk-badge--warning'}`;
  badge.textContent = gate.ok ? 'close gate: ok' : 'close gate: blocked';
  el.append(badge);
  const blocking = Array.isArray(gate.blocking) ? gate.blocking : [];
  if (!gate.ok && blocking.length) {
    const t = document.createElement('span');
    t.className = 'meta-dim';
    t.textContent = ` ${blocking.length} blocking worktree${blocking.length === 1 ? '' : 's'}: ` + blocking.map(b => {
      if (typeof b === 'string') return b;
      const where = b && (b.path || b.worktree_id) || '?';
      const problems = b && Array.isArray(b.problems) ? b.problems.join(', ') : '';
      return problems ? `${where} (${problems})` : where;
    }).join('; ');
    el.append(t);
  }
}

function renderWorktrees(task) {
  renderCloseGate(task);
  const el = byId('intake-detail-worktrees');
  if (!el) return;
  el.replaceChildren();
  const rows = Array.isArray(task.worktrees) ? task.worktrees : [];
  if (!rows.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = 'No recorded worktrees.';
    el.append(p);
    return;
  }
  const list = document.createElement('div');
  list.className = 'intake-worktrees-list';
  for (const w of rows) {
    const card = document.createElement('div');
    card.className = 'intake-worktree-card';
    card.dataset.worktreeId = w.id || '';
    const head = document.createElement('div');
    head.className = 'intake-wt-head';
    const path = document.createElement('span');
    path.className = 'code-pill intake-wt-path';
    path.textContent = w.path || '-';
    path.title = w.path || '';
    const state = document.createElement('span');
    state.className = `dk-badge dk-mono ${w.state === 'active' ? 'dk-badge--info' : 'dk-badge--neutral'}`;
    state.textContent = w.state || 'unknown';
    head.append(path, state);
    if (w.origin) {
      const origin = document.createElement('span');
      origin.className = 'dk-badge dk-mono';
      origin.textContent = w.origin;
      head.append(origin);
    }
    card.append(head);

    const st = w.status && typeof w.status === 'object' ? w.status : {};
    const chips = document.createElement('div');
    chips.className = 'intake-wt-chips';
    chips.append(
      statusChip('merged', st.merged, 'yes'),
      statusChip('present', st.present, 'no'),
      statusChip('dirty', st.dirty, 'no', st.dirty_count),
      statusChip('unpushed', st.unpushed, 'no', st.unpushed)
    );
    if (typeof st.unmerged === 'number') {
      const u = document.createElement('span');
      u.className = 'meta-dim';
      u.textContent = `unmerged commits: ${st.unmerged}`;
      chips.append(u);
    }
    card.append(chips);

    const meta = document.createElement('div');
    meta.className = 'intake-wt-meta';
    meta.append(
      wtLine('Machine', w.machine),
      wtLine('Branch', w.branch || 'unknown'),
      wtLine('Created', fmtTime(w.created_at)),
      wtLine('Status checked', w.status ? fmtTime(st.checked_at || w.status_at) : 'never (unknown)')
    );
    if (st.error) meta.append(wtLine('Status error', st.error));
    const lr = w.last_remove && typeof w.last_remove === 'object' ? w.last_remove : null;
    if (lr) meta.append(wtLine('Last remove', `${lr.result || '-'}${lr.reason ? ` · ${lr.reason}` : ''}${lr.at ? ` · ${fmtTime(lr.at)}` : ''}`));
    card.append(meta);

    const sessions = Array.isArray(w.session_ids) ? w.session_ids.filter(x => typeof x === 'string' && x) : [];
    if (sessions.length) {
      const links = document.createElement('div');
      links.className = 'intake-wt-sessions';
      const lbl = document.createElement('span');
      lbl.className = 'session-meta-item__label';
      lbl.textContent = 'Sessions';
      links.append(lbl);
      for (const sid of sessions) {
        const a = document.createElement('a');
        a.className = 'session-meta-link';
        a.href = `/sessions.html?session=${encodeURIComponent(sid)}`;
        a.textContent = sid;
        links.append(a);
      }
      card.append(links);
    }

    const pending = pendingCommands(w);
    if (pending.length) {
      const pc = document.createElement('div');
      pc.className = 'meta-dim intake-wt-pending';
      pc.textContent = 'Pending: ' + pending.map(c => `${c.kind || 'command'} ${c.state || 'queued'}${c.created_at ? ` since ${fmtTime(c.created_at)}` : ''}`).join('; ');
      card.append(pc);
    }

    if (w.state === 'active' && w.id) {
      const actions = document.createElement('div');
      actions.className = 'intake-wt-actions';
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'dk-button dk-button--secondary dk-button--sm intake-wt-remove';
      rm.textContent = 'Remove worktree';
      rm.addEventListener('click', () => removeWorktree(w));
      actions.append(rm);
      card.append(actions);
    }
    list.append(card);
  }
  el.append(list);
}

function renderCloseRefusal(body) {
  const el = byId('intake-close-refusal');
  const form = byId('intake-force-form');
  if (!el) return;
  el.replaceChildren();
  if (!body) {
    el.hidden = true;
    if (form) form.hidden = true;
    return;
  }
  el.hidden = false;
  const head = document.createElement('p');
  head.className = 'intake-close-refusal__title';
  head.textContent = 'Close refused: these worktrees still block it.';
  el.append(head);
  const ul = document.createElement('ul');
  ul.className = 'intake-close-refusal__list';
  for (const w of Array.isArray(body.worktrees) ? body.worktrees : []) {
    const li = document.createElement('li');
    const where = document.createElement('span');
    where.className = 'dk-mono';
    where.textContent = `${w.machine || '?'}:${w.path || w.worktree_id || '?'}${w.branch ? ` (${w.branch})` : ''}`;
    const problems = document.createElement('span');
    problems.className = 'meta-dim';
    problems.textContent = ` — ${Array.isArray(w.problems) && w.problems.length ? w.problems.join(', ') : 'blocked'}`;
    li.append(where, problems);
    ul.append(li);
  }
  el.append(ul);
  if (typeof body.hint === 'string' && body.hint) {
    const hint = document.createElement('p');
    hint.className = 'meta-dim';
    hint.textContent = `Hint: ${body.hint}`;
    el.append(hint);
  }
  if (form) form.hidden = false;
}

function isGateRefusal(err) {
  return err && err.status === 409 && err.body && err.body.error === 'worktree_close_gate';
}

function commandIds(resp) {
  const ids = [];
  if (resp && Array.isArray(resp.commands)) for (const c of resp.commands) if (c && c.id) ids.push(c.id);
  if (resp && resp.command && resp.command.id) ids.push(resp.command.id);
  if (resp && typeof resp.command_id === 'string') ids.push(resp.command_id);
  return [...new Set(ids)];
}

const WT_POLL_ATTEMPTS = 10;
const WT_POLL_MS = 3000;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

/* Bounded follow-up after a refresh/remove: poll the queued commands (or the
   task when the response names none), then re-render the task once more. */
async function pollWorktreeCommands(taskId, resp) {
  const status = byId('intake-worktrees-status');
  const ids = commandIds(resp);
  for (let i = 0; i < WT_POLL_ATTEMPTS && selectedTaskId === taskId; i++) {
    await pause(WT_POLL_MS);
    if (selectedTaskId !== taskId) return;
    if (ids.length) {
      const states = await Promise.all(ids.map(id => api(`/api/intake/worktree-commands/${encodeURIComponent(id)}`)
        .then(r => (r && r.command) || r || {}).catch(() => ({}))));
      const done = states.every(c => c.state === 'done' || c.state === 'failed');
      if (status) status.textContent = states.map(c => {
        const r = c.result && typeof c.result === 'object' ? c.result : {};
        return `${c.kind || 'command'}: ${c.state || 'unknown'}${r.outcome ? ` (${r.outcome}${r.reason ? `: ${r.reason}` : ''})` : ''}`;
      }).join('; ');
      if (done) break;
    } else {
      await refreshTaskDetail(taskId);
      const data = cachedDetail;
      const rows = data && Array.isArray(data.worktrees) ? data.worktrees : [];
      if (!rows.some(w => pendingCommands(w).length)) break;
    }
  }
  await refreshTaskDetail(taskId);
}

async function refreshWorktrees() {
  if (!selectedTaskId) return;
  const taskId = selectedTaskId;
  const btn = byId('intake-worktrees-refresh');
  const status = byId('intake-worktrees-status');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Queuing status check on the owning host…';
  try {
    await ensureCsrf();
    const resp = await api(`/api/intake/tasks/${encodeURIComponent(taskId)}/worktrees/refresh`, 'POST', { key: reqKey('wt') });
    if (status) status.textContent = 'Status check queued; waiting for the host…';
    await pollWorktreeCommands(taskId, resp);
  } catch (err) {
    if (status) status.textContent = `Refresh failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeWorktree(w) {
  if (!selectedTaskId || !w || !w.id) return;
  const taskId = selectedTaskId;
  const ok = window.confirm(`Remove worktree ${w.path || w.id} on ${w.machine || 'its host'}?\n\nThe host runs git worktree remove (never --force) and refuses if it is dirty, unmerged or in use.`);
  if (!ok) return;
  const status = byId('intake-worktrees-status');
  if (status) status.textContent = 'Queuing remove on the owning host…';
  try {
    await ensureCsrf();
    const resp = await api(`/api/intake/tasks/${encodeURIComponent(taskId)}/worktrees/remove`, 'POST', { key: reqKey('wt'), worktree_id: w.id });
    const refused = resp && Array.isArray(resp.refused) ? resp.refused : [];
    if (refused.length && !commandIds(resp).length) {
      if (status) status.textContent = `Remove refused by Hub: ${refused.map(r => r.reason || 'refused').join('; ')}`;
      await refreshTaskDetail(taskId);
      return;
    }
    if (status) status.textContent = 'Remove queued; waiting for the host…';
    await pollWorktreeCommands(taskId, resp);
  } catch (err) {
    if (status) status.textContent = `Remove failed: ${err.message}${err.body && err.body.reason ? ` · ${err.body.reason}` : ''}`;
  }
}

async function closeIntake(force) {
  if (!selectedTaskId || detailVersion === null) return;
  const actionStatus = byId('intake-action-status');
  const btn = byId(force ? 'intake-force-btn' : 'intake-close-btn');
  const body = { key: reqKey(force ? 'force-close' : 'close'), expected_version: detailVersion, status: 'done' };
  if (force) {
    const reason = (byId('intake-force-reason')?.value || '').trim();
    if (!reason) {
      if (actionStatus) actionStatus.textContent = 'Force close needs a reason.';
      return;
    }
    if (!window.confirm('Force close this intake despite the blocking worktrees? The reason is recorded.')) return;
    body.force = true;
    body.reason = reason;
  }
  if (btn) btn.disabled = true;
  if (actionStatus) { actionStatus.dataset.sticky = '1'; actionStatus.textContent = force ? 'Force closing…' : 'Closing…'; }
  try {
    await ensureCsrf();
    await api(`/api/intake/tasks/${encodeURIComponent(selectedTaskId)}`, 'POST', body);
    renderCloseRefusal(null);
    const reasonEl = byId('intake-force-reason');
    if (reasonEl) reasonEl.value = '';
    if (actionStatus) actionStatus.textContent = 'Intake closed.';
    await Promise.all([refreshTaskDetail(selectedTaskId), loadList().then(() => renderList(cachedTasks, byId('intake-filter')?.value || ''))]);
  } catch (err) {
    if (isGateRefusal(err)) {
      renderCloseRefusal(err.body);
      if (actionStatus) actionStatus.textContent = 'Close refused by the worktree gate.';
    } else if (actionStatus) {
      actionStatus.textContent = `Close failed: ${err.message}`;
    }
  } finally {
    if (btn) btn.disabled = force ? !(byId('intake-force-reason')?.value || '').trim() : false;
    if (actionStatus) delete actionStatus.dataset.sticky;
  }
}

function renderDetail(task) {
  const titleEl = byId('intake-detail-title');
  const badgesEl = byId('intake-detail-badges');
  const metaEl = byId('intake-detail-meta');
  const acEl = byId('intake-detail-ac');
  const sessEl = byId('intake-detail-sessions');
  const histEl = byId('intake-detail-history');
  const actionStatus = byId('intake-action-status');

  const payload = task.payload_data || {};
  const control = task.control || {};
  detailVersion = control.version ?? null;
  if (titleEl) titleEl.textContent = payload.title || task.id;
  if (badgesEl) {
    badgesEl.replaceChildren();
    badgesEl.append(statusBadge(control.status));
    if (control.continuation_state) {
      const c = document.createElement('span');
      c.className = 'dk-badge dk-badge--info dk-mono';
      c.textContent = control.continuation_state;
      badgesEl.append(c);
    }
  }
  if (metaEl) {
    metaEl.replaceChildren();
    addMetaItem(metaEl, 'Task ID', task.id);
    addMetaItem(metaEl, 'Project', task.project || '-');
    addMetaItem(metaEl, 'Status', control.status || '-');
    addMetaItem(metaEl, 'Version', detailVersion === null ? '-' : String(detailVersion));
    // Scope flag comes from the F2 project-scope registry when present;
    // older tasks carry no flag and the row is omitted (never invented).
    if (task.scope_flag !== undefined && task.scope_flag !== null) {
      addMetaItem(metaEl, 'Scope flag', String(task.scope_flag));
    }
    if (payload.owner) addMetaItem(metaEl, 'Owner', payload.owner);
  }
  if (acEl) {
    const ac = payload.acceptance_criteria;
    acEl.textContent = typeof ac === 'string' && ac ? ac : 'No acceptance criteria recorded.';
  }
  if (sessEl) {
    sessEl.replaceChildren();
    const sessions = Array.isArray(task.assigned_sessions) ? task.assigned_sessions : [];
    if (!sessions.length) {
      const p = document.createElement('p');
      p.className = 'dk-muted';
      p.textContent = 'No linked sessions.';
      sessEl.append(p);
    } else {
      const list = document.createElement('div');
      list.className = 'intake-sessions-list';
      for (const s of sessions) {
        const card = document.createElement('div');
        card.className = 'intake-session-card';
        const left = document.createElement('div');
        left.className = 'sessions-card-item__badges';
        const id = document.createElement('span');
        id.className = 'code-pill';
        id.textContent = s.hub_session_id || '-';
        id.title = s.hub_session_id || '-';
        left.append(id);
        // Real linkage/state names only; a missing or foreign row keeps its
        // recorded marker (unknown_session / not_visible), never "accepted".
        const state = document.createElement('span');
        state.className = 'dk-badge dk-mono';
        state.textContent = s.state || s.linkage || 'linked';
        left.append(state);
        if (s.machine || s.engine) {
          const extra = document.createElement('span');
          extra.className = 'meta-dim sessions-truncate';
          extra.textContent = `${s.engine || '-'} · ${s.machine || '-'}`;
          extra.title = extra.textContent;
          left.append(extra);
        }
        card.append(left);
        if (s.hub_session_id && s.linkage !== 'not_visible') {
          const open = document.createElement('a');
          open.className = 'dk-button dk-button--secondary dk-button--sm';
          open.href = `/sessions.html?session=${encodeURIComponent(s.hub_session_id)}`;
          open.textContent = 'Open session';
          card.append(open);
        }
        list.append(card);
      }
      sessEl.append(list);
    }
  }
  renderWorktrees(task);
  if (histEl) {
    histEl.replaceChildren();
    const history = Array.isArray(payload.history) ? [...payload.history].reverse() : [];
    if (!history.length) {
      const p = document.createElement('p');
      p.className = 'dk-muted';
      p.textContent = 'No history yet.';
      histEl.append(p);
    } else {
      const list = document.createElement('div');
      list.className = 'intake-history-list';
      for (const e of history.slice(0, 50)) {
        const card = document.createElement('div');
        card.className = 'intake-history-card';
        const left = document.createElement('div');
        left.className = 'sessions-card-item__badges';
        const kind = document.createElement('span');
        kind.className = 'code-pill';
        kind.textContent = e.kind || 'event';
        left.append(kind);
        const text = historyText(e);
        if (text) {
          const t = document.createElement('span');
          t.className = 'intake-history-card__text meta-dim';
          t.textContent = text.length > 220 ? `${text.slice(0, 220)}…` : text;
          t.title = text;
          left.append(t);
        }
        const right = document.createElement('span');
        right.className = 'meta-dim';
        const when = e.captured_at ? new Date(e.captured_at).toISOString().slice(0, 19).replace('T', ' ') : '';
        right.textContent = `${e.actor || ''}${when ? ` · ${when}` : ''}`;
        card.append(left, right);
        list.append(card);
      }
      histEl.append(list);
    }
  }
  // Approve surfaces only for genuinely unresolved pending approvals, and
  // resolves that exact obligation via approval_decision (operator authority).
  pendingApprovalRef = null;
  const unresolved = Array.isArray(task.unresolved) ? task.unresolved : [];
  const pending = unresolved.find(e => e && e.kind === 'pending_approval');
  const approveWrap = byId('intake-approve-wrap');
  if (pending && approveWrap) {
    pendingApprovalRef = pending.id;
    approveWrap.hidden = false;
    const t = byId('intake-approve-text');
    if (t) t.textContent = pending.text || 'This task waits for operator approval.';
  } else if (approveWrap) {
    approveWrap.hidden = true;
  }
  const closeWrap = byId('intake-close-wrap');
  if (closeWrap) closeWrap.hidden = control.status === 'done';
  const statusSelect = byId('intake-status-select');
  if (statusSelect && control.status) statusSelect.value = control.status;
  if (actionStatus && actionStatus.dataset.sticky !== '1') actionStatus.textContent = '';
}

async function loadList() {
  const filter = byId('intake-status-filter')?.value || 'open';
  const data = await api(`/api/intake/tasks?status=${encodeURIComponent(filter)}&limit=50`);
  cachedTasks = (data && Array.isArray(data.items)) ? data.items : [];
  renderList(cachedTasks, byId('intake-filter')?.value || '');
}

/* Read-only open: exactly one GET, never a session start. */
async function refreshTaskDetail(taskId) {
  if (!taskId || selectedTaskId !== taskId) return;
  const actionStatus = byId('intake-action-status');
  try {
    const data = await api(`/api/intake/tasks/${encodeURIComponent(taskId)}`);
    if (selectedTaskId !== taskId || !data || !data.task) return;
    cachedDetail = data.task;
    renderDetail(data.task);
    if (actionStatus && actionStatus.textContent === 'Loading task…') actionStatus.textContent = '';
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Could not load task: ${err.message || 'error'}`;
  }
}

function openTaskDetail(taskId, pushHistory = true) {
  if (!taskId) return;
  selectedTaskId = taskId;
  detailVersion = null;
  pendingApprovalRef = null;
  cachedDetail = null;
  renderCloseRefusal(null);
  const wtStatus = byId('intake-worktrees-status');
  if (wtStatus) wtStatus.textContent = '';
  const listSec = byId('intake-list-section');
  const detailSec = byId('intake-detail-section');
  if (listSec) listSec.hidden = true;
  if (detailSec) detailSec.hidden = false;
  if (pushHistory) history.pushState({ taskId }, '', taskPath(taskId));
  const cached = cachedTasks.find(t => t.id === taskId);
  if (cached) {
    renderDetail({ ...cached, payload_data: cached.payload_data || {}, control: cached.control || {}, unresolved: cached.unresolved || [], assigned_sessions: cached.assigned_sessions || [] });
  } else {
    const actionStatus = byId('intake-action-status');
    if (actionStatus) actionStatus.textContent = 'Loading task…';
  }
  window.scrollTo({ top: 0, behavior: 'instant' });
  refreshTaskDetail(taskId);
}

function closeTaskDetail(pushHistory = true) {
  selectedTaskId = null;
  detailVersion = null;
  pendingApprovalRef = null;
  const listSec = byId('intake-list-section');
  const detailSec = byId('intake-detail-section');
  if (detailSec) detailSec.hidden = true;
  if (listSec) listSec.hidden = false;
  if (pushHistory) history.pushState({}, '', '/intake');
  renderList(cachedTasks, byId('intake-filter')?.value || '');
}

async function ensureCsrf() {
  if (!csrf) {
    const me = await api('/auth/me');
    csrf = me.csrf_token || null;
  }
}

async function submitNote(e) {
  e.preventDefault();
  if (!selectedTaskId || detailVersion === null) return;
  const input = byId('intake-note-input');
  const btn = byId('intake-note-btn');
  const actionStatus = byId('intake-action-status');
  const text = (input?.value || '').trim();
  if (!text) {
    if (actionStatus) actionStatus.textContent = 'Enter a note before adding it.';
    return;
  }
  if (btn) btn.disabled = true;
  if (actionStatus) { actionStatus.dataset.sticky = '1'; actionStatus.textContent = 'Adding note…'; }
  try {
    await ensureCsrf();
    await api(`/api/intake/tasks/${encodeURIComponent(selectedTaskId)}/events`, 'POST', {
      key: reqKey('note'),
      expected_version: detailVersion,
      note: { text }
    });
    if (input) input.value = '';
    if (actionStatus) actionStatus.textContent = 'Note added.';
    await Promise.all([refreshTaskDetail(selectedTaskId), loadList().then(() => renderList(cachedTasks, byId('intake-filter')?.value || ''))]);
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Note failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
    if (actionStatus) delete actionStatus.dataset.sticky;
  }
}

async function submitStatus(e) {
  e.preventDefault();
  if (!selectedTaskId || detailVersion === null) return;
  const select = byId('intake-status-select');
  const btn = byId('intake-status-btn');
  const actionStatus = byId('intake-action-status');
  if (btn) btn.disabled = true;
  if (actionStatus) { actionStatus.dataset.sticky = '1'; actionStatus.textContent = 'Saving status…'; }
  try {
    await ensureCsrf();
    await api(`/api/intake/tasks/${encodeURIComponent(selectedTaskId)}`, 'POST', {
      key: reqKey('status'),
      expected_version: detailVersion,
      status: select?.value
    });
    if (actionStatus) actionStatus.textContent = 'Status saved.';
    await Promise.all([refreshTaskDetail(selectedTaskId), loadList().then(() => renderList(cachedTasks, byId('intake-filter')?.value || ''))]);
  } catch (err) {
    if (isGateRefusal(err)) renderCloseRefusal(err.body);
    if (actionStatus) actionStatus.textContent = `Status failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
    if (actionStatus) delete actionStatus.dataset.sticky;
  }
}

async function decideApproval(decision) {
  if (!selectedTaskId || detailVersion === null || !pendingApprovalRef) return;
  const actionStatus = byId('intake-action-status');
  const approveBtn = byId('intake-approve-btn');
  const denyBtn = byId('intake-deny-btn');
  if (approveBtn) approveBtn.disabled = true;
  if (denyBtn) denyBtn.disabled = true;
  if (actionStatus) { actionStatus.dataset.sticky = '1'; actionStatus.textContent = decision === 'granted' ? 'Approving…' : 'Denying…'; }
  try {
    await ensureCsrf();
    await api(`/api/intake/tasks/${encodeURIComponent(selectedTaskId)}/events`, 'POST', {
      key: reqKey('approval'),
      expected_version: detailVersion,
      event: { kind: 'approval_decision', ref: pendingApprovalRef, decision, source: { type: 'unknown' }, text: decision === 'granted' ? 'Approved from the Intake page.' : 'Denied from the Intake page.' }
    });
    if (actionStatus) actionStatus.textContent = decision === 'granted' ? 'Approved.' : 'Denied.';
    await Promise.all([refreshTaskDetail(selectedTaskId), loadList().then(() => renderList(cachedTasks, byId('intake-filter')?.value || ''))]);
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Approval failed: ${err.message}`;
  } finally {
    if (approveBtn) approveBtn.disabled = false;
    if (denyBtn) denyBtn.disabled = false;
    if (actionStatus) delete actionStatus.dataset.sticky;
  }
}

async function loadData() {
  const statusEl = byId('intake-status');
  const refreshBtn = byId('intake-refresh');
  if (refreshBtn) refreshBtn.disabled = true;
  if (statusEl) statusEl.textContent = 'Loading intake…';
  try {
    try {
      const me = await api('/auth/me');
      if (me && me.email) {
        csrf = me.csrf_token || null;
        for (const id of ['account-name', 'menu-name', 'menu-email']) {
          const el = byId(id);
          if (el) el.textContent = me.email;
        }
        const initials = document.querySelector('.dk-avatar__initials');
        if (initials) initials.textContent = me.email.slice(0, 1).toUpperCase();
      }
    } catch (e) {
      if (e && e.message === 'Sign-in required') return;
    }
    await loadList();
    if (selectedTaskId) await refreshTaskDetail(selectedTaskId);
  } catch (err) {
    if (statusEl) statusEl.textContent = 'Intake could not be refreshed.';
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function setupEvents() {
  byId('intake-filter')?.addEventListener('input', () => {
    renderList(cachedTasks, byId('intake-filter')?.value || '');
  });
  byId('intake-status-filter')?.addEventListener('change', () => {
    loadList().catch(() => {
      const statusEl = byId('intake-status');
      if (statusEl) statusEl.textContent = 'Intake could not be refreshed.';
    });
  });
  byId('intake-refresh')?.addEventListener('click', () => {
    loadData();
  });
  byId('intake-detail-back')?.addEventListener('click', () => {
    closeTaskDetail();
  });
  byId('intake-detail-refresh')?.addEventListener('click', () => {
    if (selectedTaskId) refreshTaskDetail(selectedTaskId);
  });
  byId('intake-note-form')?.addEventListener('submit', submitNote);
  byId('intake-status-form')?.addEventListener('submit', submitStatus);
  byId('intake-approve-btn')?.addEventListener('click', () => decideApproval('granted'));
  byId('intake-worktrees-refresh')?.addEventListener('click', refreshWorktrees);
  byId('intake-close-btn')?.addEventListener('click', () => closeIntake(false));
  byId('intake-force-form')?.addEventListener('submit', e => { e.preventDefault(); closeIntake(true); });
  byId('intake-force-reason')?.addEventListener('input', () => {
    const btn = byId('intake-force-btn');
    if (btn) btn.disabled = !(byId('intake-force-reason')?.value || '').trim();
  });
  byId('intake-deny-btn')?.addEventListener('click', () => decideApproval('denied'));
  byId('sign-out')?.addEventListener('click', async () => {
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', headers: csrf ? { 'X-CSRF-Token': csrf } : {} });
    } catch {}
    location.assign('/auth/login');
  });
  window.addEventListener('popstate', () => {
    const tid = taskIdFromPath();
    if (tid) openTaskDetail(tid, false);
    else if (selectedTaskId) closeTaskDetail(false);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupEvents();
  const tid = taskIdFromPath();
  loadData().then(() => {
    if (tid) openTaskDetail(tid, false);
  });
});
