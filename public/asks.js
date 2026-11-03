const status = document.getElementById('status');
const rowsEl = document.getElementById('rows');
const stateEl = document.getElementById('state');
const filterEl = document.getElementById('ask-filter');
const sourcesEl = document.getElementById('sources');
let filterTimer = null;
const node = (tag, text) => { const e = document.createElement(tag); e.textContent = text ?? ''; return e; };
const when = ms => ms ? new Date(ms).toLocaleString('en-GB', { timeZone: 'Europe/Berlin' }) : '—';
async function get(path) {
  const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  if (!r.ok) throw new Error('Request failed (' + r.status + '); check sign-in.');
  return r.json();
}
async function refresh() {
  try {
    const selected = stateEl.value;
    const queryFor = state => {
      const params = new URLSearchParams({ limit: '500' });
      if (state) params.set('state', state);
      return get('/api/asks?' + params);
    };
    let data;
    if (selected === 'unresolved') {
      const [pending, unmapped] = await Promise.all([queryFor('pending'), queryFor('unmapped')]);
      data = { asks: [...pending.asks, ...unmapped.asks].sort((a, b) => (b.ts || b.received_at || 0) - (a.ts || a.received_at || 0)), counts: pending.counts };
    } else data = await queryFor(selected);
    const c = data.counts;
    const query = (filterEl.value || '').trim().toLocaleLowerCase();
    const visible = !query ? data.asks : data.asks.filter(a =>
      `${a.text || ''} ${a.t_id || ''} ${a.intake_id || ''} ${a.agent_name || ''} ${a.agent_id || ''} ${a.state || ''}`.toLocaleLowerCase().includes(query));
    const unresolvedCount = Number(c.pending || 0) + Number(c.unmapped || 0);
    const selectedCount = selected === 'unresolved' ? unresolvedCount : selected
      ? Number(c[selected] || 0)
      : ['pending', 'unmapped', 'mapped', 'dropped_after_done', 'verified', 'not_an_ask', 'closed_unverified', 'cancelled'].reduce((sum, key) => sum + Number(c[key] || 0), 0);
    const capped = selected === 'unresolved'
      ? unresolvedCount > data.asks.length
      : selectedCount > data.asks.length;
    status.textContent = `${selected === 'unresolved' ? `${unresolvedCount} unresolved mapping asks (${c.pending} pending · ${c.unmapped} unmapped)` : `${c.unmapped} unmapped · ${c.dropped_after_done} dropped after done · ${c.pending} pending · ${c.mapped} mapped · ${c.verified} verified`} · ${c.open_gaps} unrecorded range(s) · ${c.messages} messages` +
      (query ? ` · ${visible.length} matching rows` : ` · ${visible.length} rows shown`) +
      (capped ? ' · showing at most 500 rows per selected state; this endpoint has no cursor yet' : '') +
      (data.last_tick ? ` · checked ${when(data.last_tick)}` : '');
    rowsEl.replaceChildren(...visible.map(a => {
      const card = document.createElement('article');
      card.className = 'ask-card';
      const head = document.createElement('div');
      head.className = 'ask-card__head';
      const state = node('span', a.state);
      const tone = ['pending', 'unmapped'].includes(a.state) ? 'warning'
        : ['dropped_after_done', 'closed_unverified'].includes(a.state) ? 'danger'
          : ['mapped', 'verified'].includes(a.state) ? 'success' : 'neutral';
      state.className = `dk-badge dk-badge--${tone}`;
      const source = node('span', `${a.agent_name || a.agent_id} · ${a.t_id}`);
      source.className = 'code-pill';
      head.append(state, source);
      const fullText = String(a.text ?? '');
      const preview = node('p', fullText.length > 260 ? `${fullText.slice(0, 260)}…` : fullText);
      preview.className = 'ask-card__text';
      if (fullText.length > 260) {
        const details = document.createElement('details');
        details.className = 'ask-card__verbatim';
        details.append(node('summary', 'Full verbatim ask'));
        const verbatimText = node('p', fullText);
        verbatimText.className = 'ask-card__text';
        details.append(verbatimText);
        card.append(head, preview, details);
      } else card.append(head, preview);
      const foot = document.createElement('div');
      foot.className = 'ask-card__foot';
      const target = document.createElement('span');
      if (a.intake_id) {
        const link = node('a', a.intake_id); link.href = '/intake/' + a.intake_id; target.append(link);
        if (a.ac_line != null) target.append(` · AC${a.ac_line}: ${a.ac_text || ''}`);
      } else target.textContent = a.evidence || '—';
      target.className = 'ask-card__meta';
      const time = node('time', when(a.ts || a.received_at));
      time.className = 'ask-card__meta';
      foot.append(target, time);
      card.append(foot);
      return card;
    }));
    const s = await get('/api/asks/summary');
    const rec = s.meta?.reconcile || {};
    const recNodes = [];
    if (rec.last_run) {
      const r = rec.last_run;
      recNodes.push(node('p', `Latest reconcile (${when(r.ran_at)}): cursor ${r.from_cursor}..${r.to_cursor}, scanned ${r.scanned_messages} msg(s), found ${r.found_asks} ask(s) → created ${r.created} [${(r.created_ids || []).map(id => id.slice(0, 8)).join(', ') || 'none'}], appended ${r.appended} [${(r.appended_ids || []).map(id => id.slice(0, 8)).join(', ') || 'none'}], mapped ${r.mapped}`));
    }
    sourcesEl.replaceChildren(
      ...recNodes,
      ...s.sources.map(x => node('p', `${x.source} on ${x.machine_id}: last ingest ${when(x.last_ingest_at)}${x.alerted ? ' — SILENT (alerted)' : ''}`)),
      ...s.gaps.filter(g => !g.resolved_at).map(g => node('p', `Unrecorded ${g.scope === '*' ? 'all agents' : g.scope}: t${g.from_seq}–t${g.to_seq} (${g.kind})`)));
  } catch (e) { status.textContent = e.message; }
}
function syncUrl() {
  const params = new URLSearchParams();
  if (stateEl.value !== 'unresolved') params.set('state', stateEl.value);
  if (filterEl.value.trim()) params.set('q', filterEl.value.trim());
  const query = params.toString();
  history.replaceState({}, '', `/asks.html${query ? `?${query}` : ''}`);
}
const initial = new URLSearchParams(location.search);
if (initial.has('state') && [...stateEl.options].some(option => option.value === initial.get('state'))) stateEl.value = initial.get('state');
filterEl.value = initial.get('q') || '';
stateEl.addEventListener('change', () => { syncUrl(); refresh(); });
filterEl.addEventListener('input', () => {
  syncUrl();
  clearTimeout(filterTimer);
  filterTimer = setTimeout(refresh, 200);
});
refresh();
