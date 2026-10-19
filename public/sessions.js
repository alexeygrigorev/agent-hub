/* Sessions & Lineage dashboard client */
const byId = id => document.getElementById(id);
let activeTab = 'list';
let cachedSessions = [];
let cachedTree = { tree: [] };
let csrf = null;
let loading = false;

async function api(path) {
  const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  if (r.status === 401) {
    location.assign('/auth/login');
    throw new Error('Sign-in required');
  }
  if (!r.ok) {
    throw new Error(`API request failed (${r.status})`);
  }
  return r.json();
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

function stateBadge(state) {
  const span = document.createElement('span');
  const s = state || 'unknown';
  let color = 'dk-badge--neutral';
  if (['running', 'active', 'working'].includes(s)) color = 'dk-badge--success';
  else if (['stale', 'stalled', 'uncertain', 'offered'].includes(s)) color = 'dk-badge--warning';
  else if (['failed', 'refused', 'stopped', 'offline', 'error'].includes(s)) color = 'dk-badge--danger';
  span.className = `dk-badge ${color}`;
  span.textContent = s;
  return span;
}

function truncateAttr(el, value) {
  const text = value == null || value === '' ? '-' : String(value);
  el.textContent = text;
  el.title = text;
  el.classList.add('sessions-truncate');
  return el;
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

    const badges = document.createElement('div');
    badges.className = 'sessions-card-item__badges';
    badges.append(typeBadge(s.session_type), stateBadge(s.state));
    card.append(badges);

    const name = document.createElement('div');
    name.className = 'sessions-card-item__name';
    const nameText = s.qualified_alias || s.name || s.id || '-';
    name.textContent = nameText;
    name.title = nameText;
    card.append(name);

    const meta = document.createElement('div');
    meta.className = 'sessions-card-item__meta';
    const engine = document.createElement('span');
    engine.className = 'dk-mono';
    truncateAttr(engine, s.engine || '-');
    meta.append(engine);
    const ws = document.createElement('span');
    truncateAttr(ws, s.workspace || '-');
    meta.append(ws);
    const updated = document.createElement('span');
    updated.textContent = (s.updated_at || '').slice(0, 19).replace('T', ' ') || '-';
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
    const name = (s.qualified_alias || s.name || s.id || '').toLowerCase();
    const engine = (s.engine || '').toLowerCase();
    const state = (s.state || '').toLowerCase();
    const ws = (s.workspace || '').toLowerCase();
    const type = (s.session_type || '').toLowerCase();
    return name.includes(query) || engine.includes(query) || state.includes(query) || ws.includes(query) || type.includes(query);
  });

  const emptyMessage = sessions.length === 0 ? 'No sessions reported.' : 'No sessions match filter.';
  container.append(renderSessionCards(filtered, emptyMessage));

  const columns = ['Type', 'Name / Alias', 'Engine', 'State', 'Workspace', 'Parent', 'Updated'];
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

    const tdType = document.createElement('td');
    tdType.dataset.label = 'Type';
    tdType.append(typeBadge(s.session_type));
    tr.append(tdType);

    const tdName = document.createElement('td');
    tdName.dataset.label = 'Name / Alias';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'code-pill sessions-truncate';
    const nameText = s.qualified_alias || s.name || s.id;
    nameSpan.textContent = nameText;
    nameSpan.title = nameText;
    tdName.append(nameSpan);
    tr.append(tdName);

    const tdEngine = document.createElement('td');
    tdEngine.dataset.label = 'Engine';
    tdEngine.textContent = s.engine || '-';
    tr.append(tdEngine);

    const tdState = document.createElement('td');
    tdState.dataset.label = 'State';
    tdState.append(stateBadge(s.state));
    tr.append(tdState);

    const tdWs = document.createElement('td');
    tdWs.dataset.label = 'Workspace';
    tdWs.className = 'meta-dim';
    const wsSpan = document.createElement('span');
    wsSpan.className = 'sessions-truncate';
    const wsText = s.workspace || '-';
    wsSpan.textContent = wsText;
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

    item.append(typeBadge(node.session_type));

    const nameSpan = document.createElement('span');
    nameSpan.className = 'code-pill sessions-truncate';
    const nameText = node.qualified_alias || node.name || node.id;
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
      wsSpan.textContent = node.workspace;
      wsSpan.title = node.workspace;
      item.append(wsSpan);
    }

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
    const [sessionsData, treeData] = await Promise.all([
      api('/api/sessions?limit=100'),
      api('/api/sessions/tree')
    ]);
    cachedSessions = sessionsData.sessions || [];
    cachedTree = treeData || { tree: [] };
    updateView();
    status.textContent = `Authenticated · ${cachedSessions.length} session${cachedSessions.length === 1 ? '' : 's'}`;
  } catch (err) {
    status.textContent = 'Sessions could not be refreshed.';
    updateView();
  } finally {
    loading = false;
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function updateView() {
  const filter = byId('sessions-filter')?.value || '';
  if (activeTab === 'list') {
    renderTable(cachedSessions, filter);
  } else {
    renderTree(cachedTree, filter);
  }
}

function setupEvents() {
  const listBtn = byId('view-list-btn');
  const treeBtn = byId('view-tree-btn');
  const listSec = byId('list-section');
  const treeSec = byId('tree-section');
  const filterInput = byId('sessions-filter');
  const refreshBtn = byId('sessions-refresh');
  const signOut = byId('sign-out');

  listBtn?.addEventListener('click', () => {
    activeTab = 'list';
    listBtn.setAttribute('aria-selected', 'true');
    treeBtn.setAttribute('aria-selected', 'false');
    listSec.hidden = false;
    treeSec.hidden = true;
    updateView();
  });

  treeBtn?.addEventListener('click', () => {
    activeTab = 'tree';
    treeBtn.setAttribute('aria-selected', 'true');
    listBtn.setAttribute('aria-selected', 'false');
    listSec.hidden = true;
    treeSec.hidden = false;
    updateView();
  });

  filterInput?.addEventListener('input', () => {
    updateView();
  });

  refreshBtn?.addEventListener('click', () => {
    loadData();
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
  loadData();
});
