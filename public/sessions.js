/* Sessions & Lineage dashboard client */
const byId = id => document.getElementById(id);
let activeTab = 'list';
let cachedSessions = [];
let cachedTree = { tree: [] };
// Session scope and status stay in the URL so detail/back preserves the view.
const initialParams = new URLSearchParams(location.search);
let viewFilter = initialParams.get('view') === 'attention' ? 'attention' : initialParams.get('view') === 'finished' || initialParams.get('scope') === 'all' ? 'finished' : 'running';
let showAll = viewFilter === 'finished';
let nextCursor = null;
let activeCount = null;
let attentionRows = [];
let savedListScroll = 0;

function updateMoreButton() {
  const more = byId('sessions-more');
  if (more) more.hidden = !nextCursor;
  if (more) more.textContent = 'Load more sessions';
}

async function loadMore() {
  if (!nextCursor) return;
  const data = await api(`/api/sessions?limit=50${showAll ? '' : '&live=1'}&cursor=${encodeURIComponent(nextCursor)}`);
  cachedSessions = cachedSessions.concat(data.sessions || []);
  nextCursor = data.next_cursor || null;
  updateMoreButton();
  updateView();
}

async function loadSessionPages() {
  const sessions = [];
  const known = new Set();
  const cursors = new Set();
  let cursor = null;
  let activeCount;
  do {
    const params = new URLSearchParams({ limit: '100' });
    if (!showAll) params.set('live', '1');
    if (cursor) params.set('cursor', cursor);
    const page = await api(`/api/sessions?${params.toString()}`);
    if (activeCount === undefined) activeCount = page.active_count;
    for (const session of page.sessions || []) {
      if (!known.has(session.id)) {
        known.add(session.id);
        sessions.push(session);
      }
    }
    cursor = page.next_cursor || null;
    if (showAll) break; // Finished history remains available through Load more.
    if (cursor && cursors.has(cursor)) throw new Error('Session list pagination repeated a cursor');
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return { sessions, next_cursor: cursor, active_count: activeCount };
}
let selectedSessionId = null;
let detailPollTimer = null;
let csrf = null;
let loading = false;

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
    try {
      const errBody = await r.json();
      if (errBody && errBody.error) errText = `${errBody.error} (${r.status})`;
    } catch {}
    throw new Error(errText);
  }
  return r.json();
}

function reqKey(prefix) {
  const rand = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
    ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 20)
    : `${Date.now()}${Math.floor(Math.random() * 1e6)}`;
  return `${prefix}-${rand}`;
}

function typeBadge(type) {
  const span = document.createElement('span');
  const t = type || 'aplexer';
  span.className = t === 'headless'
    ? 'dk-chip dk-mono type-badge type-badge--headless'
    : 'dk-badge dk-badge--info dk-mono type-badge type-badge--aplexer';
  span.textContent = t;
  return span;
}

// A cleanly stopped session whose worker reported done is a completion,
// not a failure (intake 17e774f8); every other stop keeps its own badge.
function sessionDisplayState(s) {
  if (s.waiting_confirmation || s.agent_state === 'waiting' || ['waiting_confirmation', 'waiting_for_human'].includes(s.state)) return 'waiting-confirmation';
  if (s.agent_state === 'idle' && s.state === 'running') return 'idle';
  if (s.state === 'stopped' && s.worker_status === 'done') return 'done';
  return s.state;
}

function sessionTaskLabel(s) {
  return s.summary || s.task_summary || s.task || s.name || s.intake_title || (s.intake_id ? `Task ${s.intake_id}` : '') || s.qualified_alias || `Session ${s.id || ''}`;
}

function attentionKind(s) {
  if (s.waiting_confirmation || s.agent_state === 'waiting' || ['waiting_confirmation', 'waiting_for_human', 'needs_human'].includes(s.state)) return 'Waiting for you';
  if (['stalled', 'blocked'].includes(s.state) || s.agent_state === 'stalled') return 'Blocked';
  if (s.state === 'uncertain') return 'Stale observation';
  if (['idle_with_open_ac', 'idle'].includes(s.state) || s.agent_state === 'idle' && s.state === 'running') return 'Idle with unfinished work';
  const updated = Date.parse(s.updated_at || '');
  if (Number.isFinite(updated) && Date.now() - updated > 15 * 60 * 1000 && !['stopped', 'done', 'failed', 'cancelled'].includes(s.state)) return 'Stale observation';
  return '';
}

function renderAttention(sessions, statusData = null) {
  const list = byId('sessions-attention-list'), count = byId('sessions-attention-count');
  if (!list) return;
  const taskTitles = new Map((Array.isArray(statusData?.tasks) ? statusData.tasks : []).map(task => [task.id, task.title]).filter(([id, title]) => id && title));
  const byIdMap = new Map(sessions.map(s => [s.id, s]));
  const authoritative = Array.isArray(statusData?.needs_attention)
    ? statusData.needs_attention.filter(row => row.source === 'session_idle' || row.source === 'session_ping' || row.source === 'session_stalled' || row.source === 'session_uncertain' || row.kind === 'session')
    : [];
  const rows = new Map();
  for (const row of authoritative) {
    const id = row.session_id || row.id;
    if (!id) continue;
    const session = { ...(byIdMap.get(id) || { id, state: row.state, updated_at: row.updated_at, intake_id: row.intake_id }) };
    if (session.intake_id && !session.intake_title) session.intake_title = taskTitles.get(session.intake_id);
    let kind = attentionKind({ ...session, ...row });
    if (!kind) kind = row.source === 'session_ping' || row.source === 'session_uncertain' ? 'Stale observation' : row.source === 'session_stalled' ? 'Blocked' : 'Idle with unfinished work';
    rows.set(id, { session, kind });
  }
  for (const session of sessions) {
    const kind = attentionKind(session);
    if (kind && !rows.has(session.id)) rows.set(session.id, { session, kind });
  }
  const values = [...rows.values()];
  list.replaceChildren();
  count.textContent = `${values.length} ${values.length === 1 ? 'session' : 'sessions'}`;
  if (!values.length) {
    const empty = document.createElement('p'); empty.className = 'dk-muted';
    empty.textContent = Array.isArray(statusData?.needs_attention) ? 'No sessions need attention.' : 'Attention status is unavailable. Refresh to try again.';
    list.append(empty); return;
  }
  for (const { session, kind } of values) {
    const item = document.createElement('article'); item.className = 'sessions-attention-item';
    const title = document.createElement('button'); title.type = 'button'; title.className = 'sessions-attention-item__title';
    title.textContent = sessionTaskLabel(session); title.addEventListener('click', () => openSessionDetail(session.id));
    const label = document.createElement('span'); label.className = 'dk-badge dk-badge--warning'; label.textContent = kind;
    const context = document.createElement('span'); context.className = 'dk-muted';
    context.textContent = session.intake_id ? `Linked task ${session.intake_id}` : `Session ${session.id}`;
    item.append(title, label, context); list.append(item);
  }
}

function stateBadge(state) {
  const span = document.createElement('span');
  const s = state || 'unknown';
  let color = 'dk-badge--neutral';
  if (['running', 'active', 'working', 'acked', 'delivered', 'done'].includes(s)) color = 'dk-badge--success';
  else if (['stale', 'stalled', 'uncertain', 'offered', 'starting', 'recorded', 'transported', 'claimed', 'held', 'waiting', 'waiting-confirmation', 'waiting_confirmation', 'waiting-idle', 'waiting_until', 'idle', 'blocked', 'waiting for you', 'idle with unfinished work', 'stale observation'].includes(s)) color = 'dk-badge--warning';
  else if (['failed', 'refused', 'stopped', 'offline', 'error', 'cancelled'].includes(s)) color = 'dk-badge--danger';
  span.className = `dk-badge ${color}`;
  span.textContent = s;
  return span;
}

function compactWorkspace(value) {
  if (value == null || value === '') return '-';
  const text = String(value);
  const parts = text.split('/').filter(Boolean);
  if (parts.length > 2) {
    return '…/' + parts.slice(-2).join('/');
  }
  return text;
}

function compactUpdated(value) {
  const clean = (value || '').slice(0, 19).replace('T', ' ');
  if (!clean) return '-';
  return clean.length >= 16 ? clean.slice(5, 16) : clean;
}

function truncateAttr(el, displayValue, fullValue) {
  const shown = displayValue == null || displayValue === '' ? '-' : String(displayValue);
  const full = fullValue == null || fullValue === '' ? shown : String(fullValue);
  el.textContent = shown;
  el.title = full;
  el.classList.add('sessions-truncate');
  return el;
}

function bindSessionTap(el, sessionId, labelText) {
  if (!sessionId) return;
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.dataset.sessionId = sessionId;
  el.setAttribute('aria-label', `Open session ${labelText || sessionId}`);
  el.addEventListener('click', e => {
    e.stopPropagation();
    openSessionDetail(sessionId);
  });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      openSessionDetail(sessionId);
    }
  });
}

function renderSessionCards(filtered, emptyMessage) {
  const cards = document.createElement('div');
  cards.className = 'sessions-cards';
  cards.setAttribute('aria-label', 'Sessions');
  if (!filtered.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = emptyMessage;
    cards.append(p);
    return cards;
  }
  for (const s of filtered) {
    const card = document.createElement('article');
    card.className = 'dk-card dk-stack sessions-card-item';
    const nameText = sessionTaskLabel(s);
    bindSessionTap(card, s.id, nameText);

    const head = document.createElement('div');
    head.className = 'sessions-card-item__head';

    const name = document.createElement('div');
    name.className = 'sessions-card-item__name';
    name.textContent = nameText;
    name.title = nameText;
    head.append(name);

    const badges = document.createElement('div');
    badges.className = 'sessions-card-item__badges';
    const chev = document.createElement('span');
    chev.className = 'sessions-card-item__chev';
    chev.setAttribute('aria-hidden', 'true');
    chev.textContent = '›';
    const displayState = sessionDisplayState(s);
    badges.append(stateBadge(attentionKind(s) || displayState), typeBadge(s.session_type), chev);
    head.append(badges);
    card.append(head);

    const meta = document.createElement('div');
    meta.className = 'sessions-card-item__meta';
    const engine = document.createElement('span');
    engine.className = 'dk-mono';
    truncateAttr(engine, s.engine || '-');
    meta.append(engine);
    const ws = document.createElement('span');
    truncateAttr(ws, compactWorkspace(s.workspace), s.workspace || '-');
    meta.append(ws);
    if (s.parent_id) {
      const parent = document.createElement('span');
      truncateAttr(parent, `↳ ${s.parent_id}`, s.parent_id);
      meta.append(parent);
    }
    const updated = document.createElement('span');
    const fullUpdated = (s.updated_at || '').slice(0, 19).replace('T', ' ') || '-';
    updated.textContent = compactUpdated(s.updated_at);
    updated.title = fullUpdated;
    meta.append(updated);
    card.append(meta);

    cards.append(card);
  }
  return cards;
}

function renderTable(sessions, filter = '') {
  const container = byId('sessions-table-container');
  container.replaceChildren();

  const query = filter.toLowerCase().trim();
  const filtered = sessions.filter(s => {
    if (!query) return true;
    const name = sessionTaskLabel(s).toLowerCase();
    const engine = (s.engine || '').toLowerCase();
    const rawState = (s.state || '').toLowerCase();
    const isWait = Boolean(s.waiting_confirmation || s.agent_state === 'waiting');
    const isIdle = Boolean(s.agent_state === 'idle' || isWait);
    const state = `${rawState} ${isWait ? 'waiting waiting-confirmation waiting_confirmation' : ''} ${isIdle ? 'idle waiting-idle' : ''}`.toLowerCase();
    const ws = (s.workspace || '').toLowerCase();
    const type = (s.session_type || '').toLowerCase();
    return name.includes(query) || engine.includes(query) || state.includes(query) || ws.includes(query) || type.includes(query);
  });

  const emptyMessage = sessions.length === 0 ? 'No sessions reported.' : 'No sessions match filter.';
  container.append(renderSessionCards(filtered, emptyMessage));

  const columns = ['Task / Session', 'Status', 'Type', 'Engine', 'Workspace', 'Parent', 'Updated'];
  const wrapper = document.createElement('div');
  wrapper.className = 'scroll sessions-table-wrap';
  const table = document.createElement('table');
  table.className = 'dk-table';

  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  for (const c of columns) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = c;
    htr.append(th);
  }
  thead.append(htr);
  table.append(thead);

  const tbody = document.createElement('tbody');
  for (const s of filtered) {
    const tr = document.createElement('tr');
    tr.className = 'sessions-row';
    const nameText = sessionTaskLabel(s);
    bindSessionTap(tr, s.id, nameText);

    const tdName = document.createElement('td');
    tdName.dataset.label = 'Task / Session';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'code-pill sessions-truncate';
    nameSpan.textContent = nameText;
    nameSpan.title = nameText;
    tdName.append(nameSpan);
    tr.append(tdName);

    const tdState = document.createElement('td');
    tdState.dataset.label = 'Status';
    const status = attentionKind(s);
    tdState.append(stateBadge(status || sessionDisplayState(s)));
    tr.append(tdState);

    const tdType = document.createElement('td');
    tdType.dataset.label = 'Type';
    tdType.append(typeBadge(s.session_type));
    tr.append(tdType);

    const tdEngine = document.createElement('td');
    tdEngine.dataset.label = 'Engine';
    tdEngine.textContent = s.engine || '-';
    tr.append(tdEngine);

    const tdWs = document.createElement('td');
    tdWs.dataset.label = 'Workspace';
    tdWs.className = 'meta-dim';
    const wsSpan = document.createElement('span');
    wsSpan.className = 'sessions-truncate';
    const wsText = s.workspace || '-';
    wsSpan.textContent = compactWorkspace(wsText);
    wsSpan.title = wsText;
    tdWs.append(wsSpan);
    tr.append(tdWs);

    const tdParent = document.createElement('td');
    tdParent.dataset.label = 'Parent';
    tdParent.className = 'meta-dim';
    tdParent.textContent = s.parent_id ? (s.parent_source === 'explicit_parent' ? s.parent_id : `${s.parent_id} (inferred)`) : 'external / root';
    tr.append(tdParent);

    const tdUpdated = document.createElement('td');
    tdUpdated.dataset.label = 'Updated';
    tdUpdated.className = 'meta-dim';
    tdUpdated.textContent = (s.updated_at || '').slice(0, 19).replace('T', ' ');
    tr.append(tdUpdated);

    tbody.append(tr);
  }

  if (filtered.length === 0) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = columns.length;
    td.textContent = emptyMessage;
    tr.append(td);
    tbody.append(tr);
  }

  table.append(tbody);
  wrapper.append(table);
  container.append(wrapper);
}

function renderTree(treeData, filter = '') {
  const container = byId('sessions-tree-container');
  container.replaceChildren();

  const roots = (treeData && Array.isArray(treeData.tree)) ? treeData.tree : [];
  if (!roots.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = 'No session hierarchy reported.';
    container.append(p);
    return;
  }

  const query = filter.toLowerCase().trim();

  function matches(node) {
    if (!query) return true;
    const name = (node.qualified_alias || node.name || node.id || '').toLowerCase();
    const engine = (node.engine || '').toLowerCase();
    const state = (node.state || '').toLowerCase();
    const ws = (node.workspace || '').toLowerCase();
    if (name.includes(query) || engine.includes(query) || state.includes(query) || ws.includes(query)) return true;
    return (node.children || []).some(matches);
  }

  function createNodeEl(node) {
    if (!matches(node)) return null;

    const li = document.createElement('li');
    li.className = 'tree-node';

    const item = document.createElement('div');
    item.className = 'tree-item';
    const nameText = node.qualified_alias || node.name || node.id;
    bindSessionTap(item, node.id, nameText);

    item.append(typeBadge(node.session_type));

    const nameSpan = document.createElement('span');
    nameSpan.className = 'code-pill sessions-truncate';
    nameSpan.textContent = nameText;
    nameSpan.title = nameText;
    item.append(nameSpan);

    item.append(stateBadge(node.state));

    if (node.engine) {
      const engSpan = document.createElement('span');
      engSpan.className = 'meta-dim dk-mono';
      engSpan.textContent = node.engine;
      item.append(engSpan);
    }

    if (node.workspace) {
      const wsSpan = document.createElement('span');
      wsSpan.className = 'meta-dim sessions-truncate';
      wsSpan.textContent = compactWorkspace(node.workspace);
      wsSpan.title = node.workspace;
      item.append(wsSpan);
    }

    const chev = document.createElement('span');
    chev.className = 'sessions-card-item__chev';
    chev.setAttribute('aria-hidden', 'true');
    chev.textContent = '›';
    item.append(chev);

    li.append(item);

    if (node.children && node.children.length > 0) {
      const childUl = document.createElement('ul');
      childUl.className = 'tree-children';
      let childCount = 0;
      for (const child of node.children) {
        const childEl = createNodeEl(child);
        if (childEl) {
          childUl.append(childEl);
          childCount++;
        }
      }
      if (childCount > 0) {
        li.append(childUl);
      }
    }

    return li;
  }

  const ul = document.createElement('ul');
  ul.className = 'tree-view';
  for (const root of roots) {
    const el = createNodeEl(root);
    if (el) ul.append(el);
  }
  if (!ul.children.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = 'No sessions match filter.';
    container.append(p);
    return;
  }
  container.append(ul);
}

function findCachedSession(sessionId) {
  const match = s => s && (s.id === sessionId || s.qualified_alias === sessionId || s.name === sessionId || (s.binding && (s.binding.native_id === sessionId || s.binding.tag === sessionId)));
  const fromList = cachedSessions.find(match);
  if (fromList) return fromList;
  function walk(nodes) {
    for (const n of nodes || []) {
      if (match(n)) return n;
      const found = walk(n.children);
      if (found) return found;
    }
    return null;
  }
  return walk(cachedTree.tree);
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

function renderSessionDetail(session, commands = []) {
  const titleEl = byId('session-detail-title');
  const badgesEl = byId('session-detail-badges');
  const metaEl = byId('session-detail-meta');
  const termOutEl = byId('session-terminal-output');
  const termMetaEl = byId('session-terminal-meta');
  const cmdsEl = byId('session-detail-commands');
  const stopBtn = byId('session-stop-btn');
  const capTailBtn = byId('session-capture-tail-btn');
  const capScreenBtn = byId('session-capture-screen-btn');
  const sendBtn = byId('session-send-btn');
  const sendInput = byId('session-send-input');

  const displayTitle = sessionTaskLabel(session);
  if (titleEl) titleEl.textContent = displayTitle;
  if (badgesEl) {
    badgesEl.replaceChildren(stateBadge(session.state === 'stopped' && session.worker_status === 'done' ? 'done' : session.state), typeBadge(session.session_type));
  }

  const isRunning = (session.lifecycle_state || session.state) === 'running';
  const isStoppable = ['offered', 'starting', 'running', 'stalled', 'uncertain'].includes(session.lifecycle_state || session.state);
  if (capTailBtn) capTailBtn.disabled = !isRunning;
  if (capScreenBtn) capScreenBtn.disabled = !isRunning;
  if (sendBtn) sendBtn.disabled = !isRunning;
  if (sendInput) sendInput.disabled = !isRunning;
  if (stopBtn) stopBtn.disabled = !isStoppable;

  if (metaEl) {
    metaEl.replaceChildren();
    addMetaItem(metaEl, 'Session ID', session.id);
    addMetaItem(metaEl, 'Alias / Name', session.qualified_alias || session.name || '-');
    if (session.intake_id) {
      const item = document.createElement('div'); item.className = 'session-meta-item';
      const label = document.createElement('span'); label.className = 'session-meta-item__label'; label.textContent = 'Linked task';
      const link = document.createElement('a'); link.className = 'session-meta-link'; link.href = `/intake/${encodeURIComponent(session.intake_id)}`; link.textContent = session.intake_title || session.intake_id;
      item.append(label, link); metaEl.append(item);
    }
    addMetaItem(metaEl, 'Engine / Mode', `${session.engine || '-'} · ${session.mode || session.session_type || '-'}`);
    addMetaItem(metaEl, 'Machine', session.machine || '-');
    addMetaItem(metaEl, 'Workspace', session.workspace || '-');
    if (session.parent_id) {
      const parentLabel = `${session.parent_id}${session.parent_source === 'explicit_parent' ? '' : ' (inferred)'}`;
      addMetaItem(metaEl, 'Parent Session', parentLabel, () => openSessionDetail(session.parent_id));
    } else {
      addMetaItem(metaEl, 'Parent Session', 'external / root');
    }
    if (session.binding && (session.binding.tag || session.binding.native_id)) {
      addMetaItem(metaEl, 'Native Binding', `${session.binding.tag || '-'} (${session.binding.native_id || '-'})`);
    }
    addMetaItem(metaEl, 'Updated', (session.updated_at || '').slice(0, 19).replace('T', ' ') || '-');
    if (session.ping && session.ping.state === 'active') {
      const ping = session.ping;
      let text = `every ${Math.round(ping.every_seconds / 60)} min, next ${String(ping.next_at || '').slice(0, 19).replace('T', ' ')}, sent ${ping.delivered_count}, skipped ${ping.skipped_count}`;
      if (ping.last_error) text += `, FAILING: ${ping.last_error}`;
      addMetaItem(metaEl, 'Ping', text);
    }
    if (session.waiting_until) {
      addMetaItem(metaEl, 'Waiting until', String(session.waiting_until).slice(0, 19).replace('T', ' ') || String(session.waiting_until));
    }
  }

  const sortedCommands = [...commands].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
  const latestCaptureWithOutput = sortedCommands.find(c => c.kind === 'capture' && c.ack && typeof c.ack.capture === 'string');
  const pendingCapture = sortedCommands.find(c => c.kind === 'capture' && ['recorded', 'transported', 'claimed'].includes(c.state));

  if (termOutEl) {
    if (latestCaptureWithOutput) {
      termOutEl.textContent = latestCaptureWithOutput.ack.capture || '(empty output)';
      if (termMetaEl) {
        const ts = (latestCaptureWithOutput.updated_at || latestCaptureWithOutput.created_at || '').slice(0, 19).replace('T', ' ');
        termMetaEl.textContent = pendingCapture
          ? `Refreshing (${pendingCapture.state}) · Last capture ${ts}`
          : `Captured ${ts}`;
      }
    } else if (pendingCapture) {
      termOutEl.textContent = `Capture command queued (${pendingCapture.state}) — waiting for host agent to report terminal output…`;
      if (termMetaEl) termMetaEl.textContent = `State: ${pendingCapture.state}`;
    } else if (isRunning) {
      termOutEl.textContent = 'No terminal capture recorded yet. Tap "Capture log" or "Capture screen" above to fetch live output from the host.';
      if (termMetaEl) termMetaEl.textContent = 'Ready to capture';
    } else {
      termOutEl.textContent = `Session is ${session.state || 'not running'} and has no recorded terminal capture.`;
      if (termMetaEl) termMetaEl.textContent = '';
    }
  }

  if (cmdsEl) {
    cmdsEl.replaceChildren();
    if (!sortedCommands.length) {
      const p = document.createElement('p');
      p.className = 'dk-muted';
      p.textContent = 'No commands sent to this session yet.';
      cmdsEl.append(p);
    } else {
      const list = document.createElement('div');
      list.className = 'session-commands-list';
      for (const c of sortedCommands.slice(0, 15)) {
        const item = document.createElement('div');
        item.className = 'session-command-card';
        const left = document.createElement('div');
        left.className = 'sessions-card-item__badges';
        const kindSpan = document.createElement('span');
        kindSpan.className = 'code-pill';
        kindSpan.textContent = c.kind;
        left.append(kindSpan, stateBadge(c.state));
        if (c.kind === 'send' && c.payload && c.payload.body) {
          const bodyPreview = document.createElement('span');
          bodyPreview.className = 'meta-dim sessions-truncate';
          bodyPreview.textContent = c.payload.body;
          bodyPreview.title = c.payload.body;
          left.append(bodyPreview);
        } else if (c.kind === 'capture' && c.payload && c.payload.mode) {
          const modeSpan = document.createElement('span');
          modeSpan.className = 'meta-dim dk-mono';
          modeSpan.textContent = c.payload.mode;
          left.append(modeSpan);
        }
        const right = document.createElement('span');
        right.className = 'meta-dim';
        const reason = c.ack && c.ack.reason ? `${c.ack.reason} · ` : '';
        right.textContent = `${reason}${(c.updated_at || c.created_at || '').slice(0, 19).replace('T', ' ')}`;
        item.append(left, right);
        list.append(item);
      }
      cmdsEl.append(list);
    }
  }
}

function activityRow(kind, main, meta) {
  const item = document.createElement('div');
  item.className = 'session-command-card';
  const left = document.createElement('div');
  left.className = 'sessions-card-item__badges';
  const kindSpan = document.createElement('span');
  kindSpan.className = 'code-pill';
  kindSpan.textContent = kind;
  left.append(kindSpan);
  const mainSpan = document.createElement('span');
  mainSpan.className = 'meta-dim sessions-truncate';
  mainSpan.textContent = main;
  mainSpan.title = main;
  left.append(mainSpan);
  const right = document.createElement('span');
  right.className = 'meta-dim';
  right.textContent = meta;
  item.append(left, right);
  return item;
}

function shortWorkKey(workKey) {
  if (!workKey || typeof workKey !== 'string') return '';
  const task = /^task:([0-9a-fA-F]{8})[0-9a-fA-F-]*$/.exec(workKey);
  if (task) return `task:${task[1]}`;
  return workKey.length > 44 ? `${workKey.slice(0, 41)}…` : workKey;
}

function renderSessionActivity(container, data) {
  container.replaceChildren();
  const claims = Array.isArray(data.claims) ? data.claims : [];
  const messages = Array.isArray(data.recent_messages) ? data.recent_messages : [];
  if (!claims.length && !messages.length) {
    const p = document.createElement('p');
    p.className = 'dk-muted';
    p.textContent = 'No active work declarations or recent bus messages in scope.';
    container.append(p);
    return;
  }
  // "Someone is already on this" banner: any live exclusive declaration.
  const activeEdits = claims.filter(c => !c.released_at && !c.stale && c.mode !== 'read' && c.mode !== 'review');
  if (activeEdits.length) {
    const banner = document.createElement('p');
    banner.className = 'dk-muted';
    const holders = [...new Set(activeEdits.map(c => c.holder || 'unknown'))].slice(0, 3).join(', ');
    banner.textContent = `Already in progress: ${activeEdits.length} active edit declaration${activeEdits.length === 1 ? '' : 's'} (${holders}). Read the rows before starting overlapping work.`;
    container.append(banner);
  }
  const list = document.createElement('div');
  list.className = 'session-commands-list';
  for (const c of claims.slice(0, 20)) {
    const scope = c.released_at ? `${c.scope} (released)` : c.stale ? `${c.scope} (stale)` : c.scope;
    const expires = c.expires_at
      ? new Date(typeof c.expires_at === 'number' ? c.expires_at : Date.parse(c.expires_at)).toISOString().slice(0, 19).replace('T', ' ')
      : '-';
    const source = c.source === 'native' ? 'native' : 'hub';
    const mode = c.mode || 'edit';
    const key = shortWorkKey(c.work_key);
    const main = `${c.holder || '-'} · ${c.project || '-'}:${scope}${key ? ` · ${key}` : ''}${c.worktree ? ` @ ${c.worktree}` : ''}`;
    const row = activityRow(`${source} ${mode}`, main, `expires ${expires}`);
    if (c.work_key) row.title = c.work_key;
    if (c.task_id) row.dataset.taskId = c.task_id;
    list.append(row);
  }
  for (const m of messages.slice(0, 20)) {
    const body = String(m.body || '').slice(0, 140);
    const created = m.created_at
      ? new Date(typeof m.created_at === 'number' ? m.created_at : Date.parse(m.created_at)).toISOString().slice(0, 19).replace('T', ' ')
      : '-';
    list.append(activityRow(m.kind || 'message', `${m.from_actor || '-'} → ${m.to_actor || '-'}: ${body}`, created));
  }
  container.append(list);
}

async function refreshSessionActivity(session) {
  const container = byId('session-detail-activity');
  const notice = byId('session-activity-notice');
  const meta = byId('session-activity-meta');
  if (!container || !notice) return;
  const sessionId = session && session.id;
  if (!session || !session.workspace) {
    notice.textContent = 'This session reports no workspace; use Fleet work activity or hub activity for hub-wide declarations.';
    container.replaceChildren();
    if (meta) meta.textContent = '';
    return;
  }
  notice.textContent = 'Loading work activity…';
  try {
    const data = await api(`/api/bus/activity?workspace=${encodeURIComponent(session.workspace)}&limit=10`);
    if (selectedSessionId !== sessionId) return;
    notice.textContent = (data && data.notice) || '';
    if (meta) {
      meta.textContent = data && data.project
        ? `claim project: ${data.project}`
        : 'no claim project mapped to this workspace';
    }
    renderSessionActivity(container, data || {});
  } catch (err) {
    if (selectedSessionId !== sessionId) return;
    notice.textContent = `Work activity could not be loaded: ${(err && err.message) || 'error'}`;
    container.replaceChildren();
    if (meta) meta.textContent = '';
  }
}

async function refreshSessionDetail(sessionId) {
  if (!sessionId || selectedSessionId !== sessionId) return;
  const actionStatus = byId('session-action-status');
  try {
    const [sessionData, cmdData] = await Promise.all([
      api(`/api/sessions/${encodeURIComponent(sessionId)}`),
      api(`/api/sessions/${encodeURIComponent(sessionId)}/commands?limit=50`).catch(() => ({ commands: [] }))
    ]);
    if (selectedSessionId !== sessionId) return;
    const cmds = (cmdData && Array.isArray(cmdData.commands)) ? cmdData.commands : [];
    renderSessionDetail(sessionData, cmds);
    refreshSessionActivity(sessionData).catch(() => {});

    const hasPending = cmds.some(c => ['recorded', 'transported', 'claimed'].includes(c.state));
    if (detailPollTimer) {
      clearTimeout(detailPollTimer);
      detailPollTimer = null;
    }
    if (hasPending) {
      // Intake 94186660: hidden tabs pause the 2s detail poll.
      detailPollTimer = setTimeout(() => { if (!document.hidden) refreshSessionDetail(sessionId); }, 2000);
    } else if (sessionData.state === 'running' && csrf && !cmds.some(c => c.kind === 'capture') && !autoCapturedSessions.has(sessionData.id)) {
      autoCapturedSessions.add(sessionData.id);
      requestCapture('tail', true);
    }
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Could not load session details: ${err.message || 'error'}`;
  }
}

function openSessionDetail(sessionId, pushHistory = true) {
  if (!sessionId) return;
  savedListScroll = window.scrollY;
  selectedSessionId = sessionId;
  if (detailPollTimer) {
    clearTimeout(detailPollTimer);
    detailPollTimer = null;
  }
  const listSec = byId('list-section');
  const treeSec = byId('tree-section');
  const detailSec = byId('session-detail-section');
  if (listSec) listSec.hidden = true;
  if (treeSec) treeSec.hidden = true;
  if (detailSec) detailSec.hidden = false;

  if (pushHistory) {
    const url = new URL(location.href);
    url.searchParams.set('session', sessionId);
    history.pushState({ sessionId }, '', url.pathname + url.search);
  }

  const actionStatus = byId('session-action-status');
  if (actionStatus) actionStatus.textContent = 'Loading session details…';

  const cached = findCachedSession(sessionId);
  if (cached) {
    renderSessionDetail(cached, []);
  }
  window.scrollTo({ top: 0, behavior: 'instant' });
  refreshSessionDetail(sessionId).then(() => {
    if (actionStatus && actionStatus.textContent === 'Loading session details…') {
      actionStatus.textContent = '';
    }
  });
}

function closeSessionDetail(pushHistory = true) {
  selectedSessionId = null;
  if (detailPollTimer) {
    clearTimeout(detailPollTimer);
    detailPollTimer = null;
  }
  const listSec = byId('list-section');
  const treeSec = byId('tree-section');
  const detailSec = byId('session-detail-section');
  if (detailSec) detailSec.hidden = true;
  if (listSec) listSec.hidden = activeTab !== 'list';
  if (treeSec) treeSec.hidden = activeTab !== 'tree';

  if (pushHistory) {
    const url = new URL(location.href);
    url.searchParams.delete('session');
    history.pushState({}, '', url.pathname + url.search);
  }
  updateView();
  window.requestAnimationFrame(() => window.scrollTo({ top: savedListScroll, behavior: 'instant' }));
}

async function requestCapture(mode = 'tail', silent = false) {
  if (!selectedSessionId) return;
  const actionStatus = byId('session-action-status');
  if (!silent && actionStatus) actionStatus.textContent = `Requesting ${mode} capture…`;
  try {
    if (!csrf) {
      const me = await api('/auth/me');
      csrf = me.csrf_token || null;
    }
    const res = await api(`/api/sessions/${encodeURIComponent(selectedSessionId)}/capture`, 'POST', {
      key: reqKey('cap'),
      mode,
      max_bytes: 65536
    });
    if (!silent && actionStatus) actionStatus.textContent = `Capture requested (${res.state || 'recorded'}).`;
    await refreshSessionDetail(selectedSessionId);
  } catch (err) {
    if (!silent && actionStatus) actionStatus.textContent = `Capture failed: ${err.message}`;
  }
}

async function requestStop() {
  if (!selectedSessionId) return;
  const actionStatus = byId('session-action-status');
  if (actionStatus) actionStatus.textContent = 'Sending stop command…';
  try {
    if (!csrf) {
      const me = await api('/auth/me');
      csrf = me.csrf_token || null;
    }
    const res = await api(`/api/sessions/${encodeURIComponent(selectedSessionId)}/stop`, 'POST', {
      key: reqKey('stop')
    });
    if (actionStatus) actionStatus.textContent = `Stop command queued (${res.state || 'recorded'}).`;
    await Promise.all([refreshSessionDetail(selectedSessionId), loadData()]);
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Stop failed: ${err.message}`;
  }
}

async function requestSend(e) {
  e.preventDefault();
  if (!selectedSessionId) return;
  const input = byId('session-send-input');
  const btn = byId('session-send-btn');
  const actionStatus = byId('session-action-status');
  const body = (input?.value || '').trim();
  if (!body) {
    if (actionStatus) actionStatus.textContent = 'Enter a message before sending.';
    return;
  }
  if (btn) btn.disabled = true;
  if (actionStatus) actionStatus.textContent = 'Sending message to session…';
  try {
    if (!csrf) {
      const me = await api('/auth/me');
      csrf = me.csrf_token || null;
    }
    const res = await api(`/api/sessions/${encodeURIComponent(selectedSessionId)}/send`, 'POST', {
      key: reqKey('send'),
      body
    });
    if (input) input.value = '';
    if (actionStatus) actionStatus.textContent = `Message queued (${res.state || 'recorded'}).`;
    await refreshSessionDetail(selectedSessionId);
  } catch (err) {
    if (actionStatus) actionStatus.textContent = `Send failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadData() {
  if (loading) return;
  loading = true;
  const status = byId('status');
  const refreshBtn = byId('sessions-refresh');
  if (refreshBtn) refreshBtn.disabled = true;
  status.textContent = 'Loading sessions…';
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
    const [sessionsData, treeData, auditData, statusData] = await Promise.all([
      loadSessionPages(),
      api(`/api/sessions/tree?limit=200${showAll ? '&include=all' : ''}`),
      // Admin-only; a 403 just means the panel stays hidden.
      api('/api/sessions/audit?last=5').catch(() => null),
      api('/api/status').catch(() => null)
    ]);
    cachedSessions = sessionsData.sessions || [];
    nextCursor = sessionsData.next_cursor || null;
    activeCount = sessionsData.active_count;
    updateMoreButton();
    cachedTree = treeData || { tree: [] };
    attentionRows = Array.isArray(statusData?.needs_attention) ? statusData.needs_attention : [];
    if (Array.isArray(statusData?.tasks)) {
      const titles = new Map(statusData.tasks.map(task => [task.id, task.title]).filter(([id, title]) => id && title));
      cachedSessions = cachedSessions.map(session => session.intake_id && titles.has(session.intake_id) ? { ...session, intake_title: titles.get(session.intake_id) } : session);
    }
    renderAttention(cachedSessions, statusData);
    renderAudit(auditData);
    updateView();
    if (selectedSessionId) {
      await refreshSessionDetail(selectedSessionId);
    }
    status.textContent = `Authenticated · ${activeCount ?? cachedSessions.length} active · ${cachedSessions.length} loaded`;
  } catch (err) {
    status.textContent = 'Sessions could not be refreshed.';
    updateView();
  } finally {
    loading = false;
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function updateView() {
  const filterInput = byId('sessions-filter');
  const filter = filterInput?.value || '';
  if (activeTab === 'list') {
    const attentionIds = new Set(attentionRows.map(row => row.session_id || row.id).filter(Boolean));
    const viewSessions = viewFilter === 'attention'
      ? cachedSessions.filter(s => attentionIds.has(s.id) || attentionKind(s))
      : viewFilter === 'finished'
        ? cachedSessions.filter(s => ['stopped', 'done', 'failed', 'cancelled', 'exited'].includes(s.state))
        : cachedSessions.filter(s => !['stopped', 'done', 'failed', 'cancelled', 'exited'].includes(s.state));
    const title = byId('sessions-list-title');
    if (title) title.textContent = viewFilter === 'attention' ? 'Sessions needing attention' : viewFilter === 'finished' ? 'Finished sessions' : 'Running sessions';
    const note = byId('sessions-scope-note');
    if (note) note.textContent = `${viewSessions.length} shown from ${cachedSessions.length} loaded · ${nextCursor ? 'more pages available' : 'no more pages reported'}`;
    document.querySelectorAll('[data-sessions-view]').forEach(link => {
      const active = link.dataset.sessionsView === viewFilter;
      link.setAttribute('aria-current', active ? 'page' : 'false');
    });
    renderTable(viewSessions, filter);
  } else {
    renderTree(cachedTree, filter);
  }
}

// Idle audit (intake 27d1e27b): scheduled continue/kill decisions and their
// outcomes. Admin-only data; any fetch failure keeps the panel hidden.
function renderAudit(data) {
  const section = byId('audit-section');
  if (!section) return;
  if (!data || !Array.isArray(data.runs)) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const summary = byId('audit-summary');
  const latest = data.runs[0];
  const counts = latest?.counts || {};
  const countText = Object.entries(counts).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
  if (summary) {
    summary.textContent = latest
      ? `Run #${latest.id} at ${latest.at.replace('T', ' ').slice(0, 19)}Z (${latest.source}) · scanned ${latest.scanned}${countText ? ' · ' + countText : ''}`
      : 'No audit run recorded yet.';
  }
  const content = byId('audit-content');
  if (!content) return;
  content.replaceChildren();
  const decisions = data.decisions || [];
  if (!decisions.length) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'scroll sessions-table-wrap';
  const table = document.createElement('table');
  table.className = 'dk-table';
  const columns = ['Time', 'Action', 'Rule', 'Session', 'Idle', 'Outcome', 'Detail'];
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  for (const c of columns) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = c;
    htr.append(th);
  }
  thead.append(htr);
  table.append(thead);
  const tbody = document.createElement('tbody');
  for (const d of decisions) {
    const tr = document.createElement('tr');
    tr.className = 'sessions-row';
    for (const key of ['at', 'action', 'rule', 'session_id', 'idle_s', 'outcome', 'detail']) {
      const td = document.createElement('td');
      const raw = d[key];
      td.textContent = raw === null || raw === undefined ? '-' : String(raw).slice(0, 160);
      if (key === 'session_id' || key === 'detail') td.className = 'meta-dim';
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);
  wrapper.append(table);
  content.append(wrapper);
}

function setupEvents() {
  const listBtn = byId('view-list-btn');
  const treeBtn = byId('view-tree-btn');
  const listSec = byId('list-section');
  const treeSec = byId('tree-section');
  const detailSec = byId('session-detail-section');
  const filterInput = byId('sessions-filter');
  const refreshBtn = byId('sessions-refresh');
  const signOut = byId('sign-out');

  listBtn?.addEventListener('click', () => {
    activeTab = 'list';
    listBtn.setAttribute('aria-selected', 'true');
    treeBtn.setAttribute('aria-selected', 'false');
    if (selectedSessionId) {
      closeSessionDetail();
    } else {
      listSec.hidden = false;
      treeSec.hidden = true;
      if (detailSec) detailSec.hidden = true;
      updateView();
    }
  });

  treeBtn?.addEventListener('click', () => {
    activeTab = 'tree';
    treeBtn.setAttribute('aria-selected', 'true');
    listBtn.setAttribute('aria-selected', 'false');
    if (selectedSessionId) {
      closeSessionDetail();
    } else {
      listSec.hidden = true;
      treeSec.hidden = false;
      if (detailSec) detailSec.hidden = true;
      updateView();
    }
  });

  filterInput?.addEventListener('input', () => {
    if (selectedSessionId) closeSessionDetail();
    const url = new URL(location.href);
    if (filterInput.value) url.searchParams.set('q', filterInput.value); else url.searchParams.delete('q');
    history.replaceState(history.state, '', url.pathname + url.search);
    if (!selectedSessionId) updateView();
  });

  refreshBtn?.addEventListener('click', () => {
    loadData();
  });
  byId('sessions-more')?.addEventListener('click', () => { loadMore().catch(() => {}); });

  byId('session-detail-back')?.addEventListener('click', () => {
    closeSessionDetail();
  });

  byId('session-detail-refresh')?.addEventListener('click', () => {
    if (selectedSessionId) refreshSessionDetail(selectedSessionId);
  });

  byId('session-capture-tail-btn')?.addEventListener('click', () => {
    requestCapture('tail', false);
  });

  byId('session-capture-screen-btn')?.addEventListener('click', () => {
    requestCapture('screen', false);
  });

  byId('session-stop-btn')?.addEventListener('click', () => {
    requestStop();
  });

  byId('session-send-form')?.addEventListener('submit', requestSend);

  byId('audit-refresh-btn')?.addEventListener('click', async () => {
    const btn = byId('audit-refresh-btn');
    if (btn) btn.disabled = true;
    try {
      renderAudit(await api('/api/sessions/audit?last=5'));
    } catch {
      renderAudit(null);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  window.addEventListener('popstate', () => {
    const sid = new URLSearchParams(location.search).get('session');
    if (sid) {
      openSessionDetail(sid, false);
    } else if (selectedSessionId) {
      closeSessionDetail(false);
    }
  });

  signOut?.addEventListener('click', async () => {
    signOut.disabled = true;
    try {
      if (!csrf) {
        const me = await api('/auth/me');
        csrf = me.csrf_token;
      }
      const res = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': csrf } });
      if (res.type !== 'opaqueredirect' && !res.ok) throw new Error('Sign-out failed');
      csrf = null;
      location.assign('/auth/login');
    } catch {
      byId('status').textContent = 'Sign-out could not be confirmed. Try again.';
      signOut.disabled = false;
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupEvents();
  const query = initialParams.get('q');
  if (query && byId('sessions-filter')) byId('sessions-filter').value = query;
  const initialSession = new URLSearchParams(location.search).get('session');
  if (initialSession) {
    openSessionDetail(initialSession, false);
  }
  loadData();
});
