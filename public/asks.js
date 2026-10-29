const status = document.getElementById('status');
const rowsEl = document.getElementById('rows');
const stateEl = document.getElementById('state');
const sourcesEl = document.getElementById('sources');
const node = (tag, text) => { const e = document.createElement(tag); e.textContent = text ?? ''; return e; };
const when = ms => ms ? new Date(ms).toLocaleString('en-GB', { timeZone: 'Europe/Berlin' }) : '—';
async function get(path) {
  const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  if (!r.ok) throw new Error('Request failed (' + r.status + '); check sign-in.');
  return r.json();
}
async function refresh() {
  const params = new URLSearchParams({ limit: '300' });
  if (stateEl.value) params.set('state', stateEl.value);
  try {
    const data = await get('/api/asks?' + params);
    const c = data.counts;
    status.textContent = `${c.unmapped} unmapped · ${c.dropped_after_done} dropped after done · ${c.pending} pending · ${c.mapped} mapped · ${c.verified} verified · ${c.open_gaps} unrecorded range(s) · ${c.messages} messages` +
      (data.last_tick ? ` · checked ${when(data.last_tick)}` : '');
    rowsEl.replaceChildren(...data.asks.map(a => {
      const tr = document.createElement('tr');
      tr.append(node('td', a.state), node('td', `${a.agent_name || a.agent_id} ${a.t_id}`));
      const text = node('td', a.text); text.style.whiteSpace = 'pre-wrap'; tr.append(text);
      const target = document.createElement('td');
      if (a.intake_id) {
        const link = node('a', a.intake_id.slice(0, 8)); link.href = '/intake/' + a.intake_id; target.append(link);
        if (a.ac_line != null) target.append(` · AC${a.ac_line}: ${a.ac_text || ''}`);
      } else target.textContent = a.evidence || '—';
      tr.append(target, node('td', when(a.ts || a.received_at)));
      return tr;
    }));
    const s = await get('/api/asks/summary');
    sourcesEl.replaceChildren(
      ...s.sources.map(x => node('p', `${x.source} on ${x.machine_id}: last ingest ${when(x.last_ingest_at)}${x.alerted ? ' — SILENT (alerted)' : ''}`)),
      ...s.gaps.filter(g => !g.resolved_at).map(g => node('p', `Unrecorded ${g.scope === '*' ? 'all agents' : g.scope}: t${g.from_seq}–t${g.to_seq} (${g.kind})`)));
  } catch (e) { status.textContent = e.message; }
}
stateEl.addEventListener('change', refresh);
refresh();
