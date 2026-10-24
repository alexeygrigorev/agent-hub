/* Render server data as text; never inject HTML, transcripts or credentials. */
const byId = id => document.getElementById(id);
let csrf = null;
let refreshing = false;

function table(columns, rows) {
  const wrapper = document.createElement('div');
  wrapper.className = 'scroll';
  const t = document.createElement('table');
  t.className = 'dk-table';
  const head = document.createElement('tr');
  for (const title of columns) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = title;
    head.append(th);
  }
  const thead = document.createElement('thead');
  thead.append(head);
  t.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const [index, text] of row.entries()) {
      const td = document.createElement('td');
      td.dataset.label = columns[index];
      td.textContent = text ?? 'unknown';
      tr.append(td);
    }
    body.append(tr);
  }
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = columns.length;
    cell.textContent = 'No records reported.';
    row.append(cell);
    body.append(row);
  }
  t.append(body);
  wrapper.append(t);
  return wrapper;
}

function shown(value) {
  return value === null || value === undefined ? 'unknown' : String(value);
}

async function api(path) {
  const headers = {};
  if (csrf) headers['X-CSRF-Token'] = csrf;
  const r = await fetch(path, { method: 'GET', headers, credentials: 'same-origin', cache: 'no-store' });
  if (r.status === 401) {
    csrf = null;
    const err = Error('expired');
    err.code = 'expired';
    throw err;
  }
  if (r.status === 403) {
    const err = Error('access_denied');
    err.code = 'access_denied';
    throw err;
  }
  if (r.status === 404) {
    // Expected detail 404 (unknown/unavailable) is not a service failure.
    // Return the DTO so showHost can render bounded unavailable state.
    let body = null;
    try { body = await r.json(); } catch {}
    if (body && (body.status === 'unknown' || body.error === 'unavailable')) return body;
    const err = Error('service_failure');
    err.code = 'service_failure';
    throw err;
  }
  if (!r.ok) {
    const err = Error('service_failure');
    err.code = 'service_failure';
    throw err;
  }
  return r.json();
}

function coverageLabel(code) {
  if (code === 'observed') return 'observed';
  if (code === 'partial') return 'partial (unknown, not zero)';
  if (code === 'missing') return 'missing';
  if (code === 'unsupported') return 'unsupported';
  return 'unknown';
}

function renderEmpty(reason) {
  HubMetricsCharts.clear(reason);
  byId('coverage').replaceChildren(Object.assign(document.createElement('p'), { textContent: reason }));
  byId('buckets').replaceChildren(Object.assign(document.createElement('p'), { textContent: 'No 24h buckets.' }));
  byId('usage').replaceChildren(Object.assign(document.createElement('p'), { textContent: 'Usage unknown.' }));
  byId('accepted').replaceChildren(Object.assign(document.createElement('p'), { textContent: 'Accepted outcomes unknown.' }));
}

function render(data) {
  if (!data || data.status === 'unknown' || (data.error === 'unavailable' && !data.as_of)) {
    renderEmpty('No sanitized snapshot for this host.');
    return;
  }
  HubMetricsCharts.render(data);
  const stale = data.stale === true || data.status === 'stale';
  const cov = data.coverage || {};
  const gaps = Array.isArray(data.gaps) ? data.gaps.map(g => g && g.code).filter(Boolean) : [];
  byId('coverage').replaceChildren(table(
    ['Surface', 'Coverage', 'Window'],
    [
      ['Utilization', coverageLabel(cov.utilization), shown(data.as_of)],
      ['Usage', coverageLabel(cov.usage), shown(data.as_of)],
      ['Accepted outcomes', coverageLabel(cov.accepted), shown(data.as_of)],
      ['Gaps', gaps.length ? gaps.join(', ') : 'none', stale ? 'stale' : 'current'],
    ]
  ));

  const projects = (data.utilization && data.utilization.projects) || {};
  const bucketRows = [];
  for (const [project, body] of Object.entries(projects)) {
    const unknown = !body || body.unknown === true;
    const buckets = Array.isArray(body && body.hourly_buckets) ? body.hourly_buckets : [];
    if (!buckets.length) {
      bucketRows.push([project, 'unknown', 'unknown', unknown ? 'unknown' : shown(body && body.coverage)]);
      continue;
    }
    for (const bucket of buckets) {
      bucketRows.push([
        project,
        shown(bucket && bucket.bucket_start),
        unknown ? 'unknown' : shown(bucket && bucket.active_agents),
        unknown ? 'unknown' : shown(bucket && bucket.agent_hours),
      ]);
    }
  }
  byId('buckets').replaceChildren(table(['Project', 'Bucket start (UTC)', 'Active agents', 'Agent hours'], bucketRows));

  const usageProjects = (data.usage && data.usage.projects) || {};
  const usageRows = Object.entries(usageProjects).map(([project, body]) => {
    const unknown = !body || body.unknown === true;
    return [
      project,
      unknown ? 'unknown' : shown(body.total_input_tokens),
      unknown ? 'unknown' : shown(body.total_output_tokens),
      unknown ? 'unknown' : shown(body.total_cache_read_tokens),
      unknown ? 'unknown' : shown(body.total_cache_creation_tokens),
      unknown ? 'unknown' : shown(body.total_reasoning_tokens),
      unknown ? 'unknown' : shown(body.total_noncache_input_tokens),
      body && body.has_unknown_tokens ? 'unknown tokens present' : (unknown ? 'unknown' : 'known'),
      body && body.has_unknown_cache ? 'cache coverage unknown' : shown(body && body.cache_coverage),
      body && body.has_unknown_reasoning ? 'reasoning coverage unknown' : shown(body && body.reasoning_coverage),
    ];
  });
  byId('usage').replaceChildren(table(
    ['Project', 'Input tokens', 'Output tokens', 'Cache read', 'Cache creation', 'Reasoning', 'Noncache input', 'Token coverage', 'Cache coverage', 'Reasoning coverage'],
    usageRows
  ));

  const accepted = data.accepted || {};
  const acceptedUnknown = accepted.unknown === true;
  const acceptedRows = [
    ['all', acceptedUnknown ? 'unknown' : shown(accepted.accepted_outcomes), acceptedUnknown ? 'unknown' : shown(accepted.task_count), acceptedUnknown ? 'unknown' : shown(accepted.commit_count), acceptedUnknown ? 'unknown' : shown(accepted.review_count)],
  ];
  for (const [project, body] of Object.entries(accepted.projects || {})) {
    const unknown = !body || body.unknown === true;
    acceptedRows.push([
      project,
      unknown ? 'unknown' : shown(body.accepted_outcomes),
      unknown ? 'unknown' : shown(body.task_count),
      unknown ? 'unknown' : shown(body.commit_count),
      unknown ? 'unknown' : shown(body.review_count),
    ]);
  }
  byId('accepted').replaceChildren(table(
    ['Project', 'Accepted outcomes', 'Tasks', 'Commits', 'Reviews'],
    acceptedRows
  ));
}

const HOST_ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;
let selectedHostId = null;
let listedHosts = [];
let nextHostCursor = null;
// R5 R5: every detail fetch carries the generation of its selection. Awaited
// responses commit only when they are still both the latest request AND the
// current selection, across success and error paths, so a delayed B response
// (or its error) can never overwrite newer C label/status/data.

function setHostIdentity(hostId, missing) {
  const label = byId('metrics-host-id');
  if (!hostId) {
    label.textContent = missing ? 'Selected host is no longer available.' : 'No host selected.';
    return;
  }
  label.textContent = missing ? `Host ${hostId} is no longer available.` : `Host ${hostId}`;
}

function fillHostSelect() {
  const select = byId('metrics-host');
  select.replaceChildren();
  if (!listedHosts.length) {
    // R4 F4: an empty listing with a preserved selection keeps that identity
    // represented (unavailable) instead of silently dropping to No hosts.
    // Initial empty state with no prior selection stays empty.
    if (selectedHostId) {
      const opt = document.createElement('option');
      opt.value = selectedHostId;
      opt.textContent = selectedHostId + ' (unavailable)';
      opt.selected = true;
      select.append(opt);
      select.disabled = true;
      return;
    }
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'No hosts';
    select.append(opt);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const hostId of listedHosts) {
    const opt = document.createElement('option');
    opt.value = hostId;
    opt.textContent = hostId;
    if (hostId === selectedHostId) opt.selected = true;
    select.append(opt);
  }
  if (selectedHostId && !listedHosts.includes(selectedHostId)) {
    const opt = document.createElement('option');
    opt.value = selectedHostId;
    opt.textContent = selectedHostId + ' (unavailable)';
    opt.selected = true;
    select.append(opt);
  }
}

async function loadHostPage(after) {
  const query = after ? `/api/metrics?limit=8&after=${encodeURIComponent(after)}` : '/api/metrics?limit=8';
  const listing = await api(query);
  const items = Array.isArray(listing.items) ? listing.items : [];
  for (const item of items) {
    if (item && typeof item.host_id === 'string' && HOST_ID_RE.test(item.host_id) && !listedHosts.includes(item.host_id)) {
      listedHosts.push(item.host_id);
    }
  }
  nextHostCursor = typeof listing.next_cursor === 'string' && HOST_ID_RE.test(listing.next_cursor) ? listing.next_cursor : null;
  byId('metrics-host-more').hidden = !nextHostCursor;
  return listing;
}

let detailGeneration = 0;
// R6: every paged host-list refresh carries its own generation. Awaited page
// responses commit only when no newer refresh started AND the live user
// selection at commit is reconciled (not a stale value captured at refresh
// start), across success and page-failure paths, so a delayed B page (or its
// 503) can never overwrite/clear newer C label/status/data.
let refreshGeneration = 0;

function liveSelectedHost() {
  return selectedHostId && HOST_ID_RE.test(selectedHostId) ? selectedHostId : null;
}

function detailIsCurrent(gen, hostId) {
  return gen === detailGeneration && selectedHostId === hostId;
}

async function showHost(hostId) {
  const gen = ++detailGeneration;
  selectedHostId = hostId;
  fillHostSelect();
  if (!hostId) {
    setHostIdentity(null);
    renderEmpty('No host has posted a sanitized snapshot.');
    byId('status').textContent = 'Authenticated · empty';
    return;
  }
  let snap;
  try {
    snap = await api('/api/metrics?host_id=' + encodeURIComponent(hostId));
  } catch (e) {
    // A superseded selection's error is swallowed: it must not overwrite the
    // newer selection's label/status/data. Only the current selection's error
    // propagates to the caller's handler (unavailable/expired/denied/service).
    if (!detailIsCurrent(gen, hostId)) return;
    throw e;
  }
  // A superseded selection's success is ignored for the same reason.
  if (!detailIsCurrent(gen, hostId)) return;
  const missing = !snap || snap.status === 'unknown' || (snap.error === 'unavailable' && !snap.as_of);
  setHostIdentity(hostId, missing);
  render(snap);
  if (missing) byId('status').textContent = `Authenticated · snapshot unknown · ${hostId}`;
  else if (snap.stale) byId('status').textContent = `Authenticated · snapshot is stale · ${hostId}`;
  else byId('status').textContent = `Authenticated · as of ${snap.as_of || 'unknown'} · ${hostId}`;
}

function applyError(code) {
  if (code === 'expired' || code === 'access_denied') HubHostCharts.clear();
  if (code === 'expired') {
    byId('status').textContent = 'Session expired. Sign in again.';
    renderEmpty('Session expired.');
    location.assign('/auth/login');
    return;
  }
  if (code === 'access_denied') {
    byId('status').textContent = 'Access denied.';
    renderEmpty('Access denied.');
    return;
  }
  byId('status').textContent = 'Metrics service failed.';
  renderEmpty('Service failure.');
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  byId('metrics-refresh').disabled = true;
  const myRefresh = ++refreshGeneration;
  const startSelected = liveSelectedHost();
  try {
    const me = await api('/auth/me');
    if (myRefresh !== refreshGeneration) return;
    csrf = me.csrf_token;
    HubHostCharts.refresh(api);
    for (const id of ['account-name', 'menu-name', 'menu-email']) byId(id).textContent = me.email;
    document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0, 1).toUpperCase();
    listedHosts = [];
    nextHostCursor = null;
    const listing = await loadHostPage(null);
    if (myRefresh !== refreshGeneration) return;
    // R6: the selection may have changed during ANY awaited page above or
    // below. Re-read the LIVE selection here and on every page iteration so
    // the tail below never restores a stale one captured at refresh start.
    // Preserve selected identity while resolving later pages.
    let guard = 0;
    while (nextHostCursor && guard++ < 32) {
      const live = liveSelectedHost();
      if (!live || listedHosts.includes(live)) break;
      await loadHostPage(nextHostCursor);
      if (myRefresh !== refreshGeneration) return;
    }
    if (myRefresh !== refreshGeneration) return;
    // R6 commit: reconcile host options using the CURRENT selection at
    // commit, not a stale value from refresh start.
    const commitSelected = liveSelectedHost();
    const items = Array.isArray(listing.items) ? listing.items : [];
    if (!items.length && listing.host_id && HOST_ID_RE.test(listing.host_id)) {
      selectedHostId = listing.host_id;
      fillHostSelect();
      setHostIdentity(listing.host_id);
      render(listing);
      byId('status').textContent = listing.stale ? `Authenticated · snapshot is stale · ${listing.host_id}` : `Authenticated · as of ${listing.as_of || 'unknown'} · ${listing.host_id}`;
      return;
    }
    if (!listedHosts.length) {
      // No hosts listed: still try preserved selection detail before empty.
      if (commitSelected) {
        try {
          await showHost(commitSelected);
          return;
        } catch (e) {
          if (myRefresh !== refreshGeneration) return;
          if (e && e.code === 'unavailable') {
            selectedHostId = commitSelected;
            fillHostSelect();
            setHostIdentity(commitSelected, true);
            renderEmpty('Selected host is no longer available.');
            byId('status').textContent = `Authenticated · snapshot unknown · ${commitSelected}`;
            return;
          }
          throw e;
        }
      }
      selectedHostId = null;
      fillHostSelect();
      setHostIdentity(null);
      renderEmpty('No host has posted a sanitized snapshot.');
      byId('status').textContent = 'Authenticated · empty';
      return;
    }
    // Keep selected identity if still valid or removed (show unavailable);
    // only fall back to first page host when no prior selection exists.
    // commitSelected (live at commit, not pre-await) wins: a selection made
    // during refresh must survive it.
    const keep = commitSelected || listedHosts[0];
    await showHost(keep);
  } catch (e) {
    if (myRefresh !== refreshGeneration) return;
    const code = e && e.code;
    const live = liveSelectedHost();
    // R6: a stale page failure must not clear a newer selection's committed
    // detail. Reconcile options truthfully (partial list + preserved
    // selection) and keep the current C data/status. Auth failures still
    // surface; initial-empty still falls through to empty/error.
    if (code === 'service_failure' && live && live !== startSelected) {
      fillHostSelect();
      return;
    }
    applyError(code);
  } finally {
    refreshing = false;
    byId('metrics-refresh').disabled = false;
  }
}

byId('metrics-refresh').addEventListener('click', refresh);
byId('metrics-host').addEventListener('change', () => {
  const hostId = byId('metrics-host').value;
  if (!hostId || !HOST_ID_RE.test(hostId)) return;
  showHost(hostId).catch((e) => applyError(e && e.code));
});
byId('metrics-host-more').addEventListener('click', () => {
  if (!nextHostCursor) return;
  loadHostPage(nextHostCursor).then(() => {
    fillHostSelect();
  }).catch((e) => applyError(e && e.code));
});
const signOut = byId('sign-out');
if (signOut) signOut.addEventListener('click', async () => {
  signOut.disabled = true;
  try {
    const me = await api('/auth/me');
    const response = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: { 'X-CSRF-Token': me.csrf_token } });
    if (response.type !== 'opaqueredirect' && !response.ok) throw Error('Sign-out failed');
    csrf = null;
    HubHostCharts.clear();
    location.assign('/auth/login');
  } catch {
    byId('status').textContent = 'Sign-out could not be confirmed. Try again.';
    signOut.disabled = false;
  }
});
refresh();
