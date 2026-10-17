/* Sessions & Lineage dashboard client */
const byId = id => document.getElementById(id);
let activeTab = 'list';
let cachedSessions = [];
let cachedTree = [];

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
  span.className = `type-badge type-badge--${t === 'headless' ? 'headless' : 'aplexer'}`;
  span.textContent = t;
  return span;
}

function stateBadge(state) {
  const span = document.createElement('span');
  const s = state || 'unknown';
  let color = 'dk-badge--neutral';
  if (['running', 'active'].includes(s)) color = 'dk-badge--success';
  else if (['failed', 'refused', 'stopped'].includes(s)) color = 'dk-badge--danger';
  span.className = `dk-badge ${color}`;
  span.textContent = s;
  return span;
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

  const columns = ['Type', 'Name / Alias', 'Engine', 'State', 'Workspace', 'Parent', 'Updated'];
  const wrapper = document.createElement('div');
  wrapper.className = 'scroll';
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
    nameSpan.className = 'code-pill';
    nameSpan.textContent = s.qualified_alias || s.name || s.id;
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
    tdWs.textContent = s.workspace || '-';
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
    td.textContent = sessions.length === 0 ? 'No sessions found.' : 'No sessions match filter.';
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

  const roots = treeData.tree || [];
  if (!roots.length) {
    const p = document.createElement('p');
    p.textContent = 'No session hierarchy found.';
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
    nameSpan.className = 'code-pill';
    nameSpan.textContent = node.qualified_alias || node.name || node.id;
    item.append(nameSpan);

    item.append(stateBadge(node.state));

    if (node.engine) {
      const engSpan = document.createElement('span');
      engSpan.className = 'meta-dim';
      engSpan.textContent = `[${node.engine}]`;
      item.append(engSpan);
    }

    if (node.workspace) {
      const wsSpan = document.createElement('span');
      wsSpan.className = 'meta-dim';
      wsSpan.textContent = node.workspace;
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
  container.append(ul);
}

async function loadData() {
  const status = byId('status');
  status.textContent = 'Loading sessions…';
  try {
    const [sessionsData, treeData] = await Promise.all([
      api('/api/sessions?limit=100'),
      api('/api/sessions/tree')
    ]);
    cachedSessions = sessionsData.sessions || [];
    cachedTree = treeData || { tree: [] };
    updateView();
    status.textContent = `${cachedSessions.length} session${cachedSessions.length === 1 ? '' : 's'}`;
  } catch (err) {
    status.textContent = 'Error loading sessions';
    console.error(err);
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

  listBtn?.addEventListener('click', () => {
    activeTab = 'list';
    listBtn.classList.add('is-active');
    listBtn.setAttribute('aria-selected', 'true');
    treeBtn.classList.remove('is-active');
    treeBtn.setAttribute('aria-selected', 'false');
    listSec.hidden = false;
    treeSec.hidden = true;
    updateView();
  });

  treeBtn?.addEventListener('click', () => {
    activeTab = 'tree';
    treeBtn.classList.add('is-active');
    treeBtn.setAttribute('aria-selected', 'true');
    listBtn.classList.remove('is-active');
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
}

document.addEventListener('DOMContentLoaded', () => {
  setupEvents();
  loadData();
});
