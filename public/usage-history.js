/* Quota-window and token usage history. No measurements are invented:
 * missing points stay gaps, unknown reset times draw no markers, and engines
 * without a native token log are labelled per engine with the reason.
 * Depends on HubMetricsCharts.timeSeries (metrics-charts.js). */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, text, cls) => Object.assign(document.createElement(tag), { textContent: text || '', className: cls || '' });
  const RESET_SUFFIX = '__reset_at';
  // Canonical account aliases come from the shared quota endpoint.
  let accountAliases = {}, accountLabels = {}, engineAliases = {};
  const canonicalAccount = label => accountAliases[label] || label;
  const displayAccount = label => accountLabels[accountAliases[label] || label] || accountAliases[label] || label;
  let fetchApi = null, generation = 0, initialized = false;
  let range = '24h';
  const root = () => document.getElementById('usage-history');
  const quotaRoot = () => document.getElementById('usage-quota-history');
  const tokenRoot = () => document.getElementById('usage-token-history');
  const sourceRoot = () => document.getElementById('usage-token-sources');

  function statusLine(data) {
    return `${data.range} · sampled every minute (quota collection every 10 min) · retained 32 days · older points downsampled · gaps are unknown`;
  }

  function windowBase(metric) {
    return metric.endsWith(RESET_SUFFIX) ? metric.slice(0, -RESET_SUFFIX.length) : metric;
  }

  function resetMarkers(resets) {
    // resets: sorted points {at, value}; a marker fires when a known reset
    // time differs from the previous known one (a new window began).
    // Values are provider-reported reset instants; observation timestamps
    // only tell us when a collector learned about that reset.
    return [...new Set(resets.filter(p => typeof p.value === 'number' && Number.isFinite(p.value))
      .map(p => p.value))].sort((a,b)=>a-b);
  }

  function decorate(svg, marks, start, end) {
    if (!marks.length) return 0;
    const W = matchMedia('(max-width: 600px)').matches ? 340 : 760;
    const L = 46, R = 26, T = 18, B = 48, H = 250;
    const x = at => L + (at - start) / (end - start) * (W - L - R);
    let added = 0;
    for (const at of marks) {
      if (at < start || at > end) continue;
      const line = document.createElementNS(NS, 'line');
      line.setAttribute('x1', x(at)); line.setAttribute('x2', x(at));
      line.setAttribute('y1', T); line.setAttribute('y2', H - B);
      line.setAttribute('class', 'metrics-reset');
      const title = document.createElementNS(NS, 'title');
      title.textContent = `Provider-reported window reset at ${new Date(at).toISOString()}`;
      line.append(title);
      svg.append(line);
      added++;
    }
    return added;
  }

  function drawQuota(data, host) {
    const hostDiv = quotaRoot();
    hostDiv.replaceChildren();
    const start = Date.parse(data.start), end = Date.parse(data.end);
    const quota = (data.series || []).filter(s => s.kind === 'quota_window' && (!host || !s.machine_id || s.machine_id === host));
    const byAccount = new Map();
    for (const s of quota) {
      const account = canonicalAccount(s.entity);
      if (!byAccount.has(account)) byAccount.set(account, []);
      byAccount.get(account).push(s);
    }
    if (!byAccount.size) {
      hostDiv.append(el('p', 'No per-window quota history in this range yet. History accumulates from every quota collection; null stays a gap.', 'metrics-chart-empty'));
      return;
    }
    for (const [account, rows] of byAccount) {
      const merged = mergeMetrics(rows);
      const lines = merged.filter(s => !s.metric.endsWith(RESET_SUFFIX));
      // Windows with no known points leave no trace: an empty/null line is
      // not a chart (AC2 renders with real data, never only a null line).
      const real = lines.filter(s => s.points.some(p => typeof p.value === 'number' && Number.isFinite(p.value)));
      if (!real.length) continue;
      const resetsByWindow = new Map();
      for (const s of merged.filter(s => s.metric.endsWith(RESET_SUFFIX))) {
        resetsByWindow.set(windowBase(s.metric), [...s.points].sort((a, b) => a.at - b.at));
      }
      const series = real.map(s => ({
        label: `${windowBase(s.metric)} · ${s.points.filter(p => typeof p.value === 'number').length} known`,
        points: s.points,
      }));
      const figure = HubMetricsCharts.timeSeries(`${displayAccount(account)} · Quota remaining by window`, series, start, end, ' %');
      const svg = figure.querySelector('svg');
      let marks = 0;
      if (svg) {
        for (const [window, points] of resetsByWindow) {
          void window;
          marks += decorate(svg, resetMarkers(points), start, end);
        }
      }
      if (marks > 0) {
        figure.append(el('p', `${marks} window reset marker${marks === 1 ? '' : 's'} (vertical line) at provider-reported reset times; values across a reset are not comparable.`, 'metrics-chart-note'));
      }
      hostDiv.append(figure);
    }
    hostDiv.append(el('p', 'One line per window (5h / 7d / monthly where the provider reports it). ' +
      'Reset markers come from the provider-reported reset time; unknown resets draw no markers.', 'metrics-chart-note'));
  }

  function mergeMetrics(rows) {
    const metrics = new Map();
    for (const row of rows) {
      if (!metrics.has(row.metric)) metrics.set(row.metric, new Map());
      const points = metrics.get(row.metric);
      for (const p of row.points) {
        // Merge alias null grids by bucket, while preserving actual sample times.
        const key = p.bucket_at ?? p.at;
        const old = points.get(key);
        const observed = Date.parse(p.recorded_at || p.observed_at || '');
        const prior = Date.parse(old?.recorded_at || old?.observed_at || '');
        if (!old || (Number.isFinite(observed) && (!Number.isFinite(prior) || observed > prior))) points.set(key, p);
      }
    }
    return [...metrics].map(([metric, points]) => ({ metric, points: [...points.values()].sort((a, b) => a.at - b.at) }));
  }

  function drawTokens(data, sources) {
    const host = tokenRoot();
    host.replaceChildren();
    const start = Date.parse(data.start), end = Date.parse(data.end);
    const tokens = (data.series || []).filter(s => s.kind === 'tokens');
    const byEntity = new Map();
    for (const s of tokens) {
      const [engine, account, project] = s.entity.split(' / ');
      const entity = project ? `${displayAccount(account)} / ${project}` : s.entity;
      const harness = engineAliases[engine] || engine;
      const key = `${s.machine_id || ''}\0${harness}\0${project ? canonicalAccount(account) + '\0' + project : s.entity}`;
      if (!byEntity.has(key)) byEntity.set(key, { entity, rows: [] });
      byEntity.get(key).rows.push(s);
    }
    if (!byEntity.size) {
      host.append(el('p', 'No native token history in this range yet. Hourly collection posts complete hours only; absence is unknown, never zero.', 'metrics-chart-empty'));
    }
    for (const { entity, rows } of byEntity.values()) {
      const metrics = new Map(mergeMetrics(rows).map(s => [s.metric, s.points]));
      const series = [];
      for (const metric of ['input_tokens', 'output_tokens']) {
        if (metrics.has(metric)) series.push({ label: `${metric === 'input_tokens' ? 'Input' : 'Output'} · ${metrics.get(metric).filter(p => typeof p.value === 'number').length} known hours`, points: metrics.get(metric) });
      }
      if (series.length) host.append(HubMetricsCharts.timeSeries(`${entity} · Tokens per hour`, series, start, end, ''));
    }
    const notes = sourceRoot();
    notes.replaceChildren();
    if (Array.isArray(sources)) {
      const items = el('ul', '', 'metrics-legend');
      for (const s of sources) {
        items.append(el('li', `${s.engine}: ${s.supported ? 'native history — ' + s.detail : 'not collected — ' + s.detail}`));
      }
      notes.append(el('p', 'Per-engine token provenance (quota percentages are never converted into tokens):', 'metrics-chart-note'), items);
    }
  }

  const berlin = value => {
    const at = typeof value === 'number' ? value : Date.parse(value || '');
    return Number.isFinite(at) ? new Intl.DateTimeFormat('en-GB', {
      timeZone:'Europe/Berlin', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', timeZoneName:'short',
    }).format(at) : 'Unknown';
  };
  function drawCurrent(quota, banked) {
    const host = document.getElementById('usage-current');
    if (!host) return;
    host.replaceChildren();
    if (!quota?.pools?.length) { host.append(el('p','Current quota unavailable.')); return; }
    const scroll = el('div', '', 'usage-current-scroll');
    scroll.setAttribute('tabindex','0'); scroll.setAttribute('aria-label','Current usage by account and engine');
    const table = el('table','','dk-table usage-current-table');
    const head = el('thead'), titles = el('tr');
    for (const title of ['Account / engine', '5h used', 'Weekly used', 'Next resets · Berlin', 'Credits balance', 'Banked resets']) {
      const th=el('th',title); th.scope='col'; titles.append(th);
    }
    head.append(titles); const body=el('tbody');
    const now=Date.now();
    for (const pool of quota.pools) {
      const tr=el('tr'), windows=pool.windows || {};
      const five=windows['5h'] || windows.five_hour || windows['5hour'];
      const week=windows['7d'] || windows.weekly || windows.week;
      const used=w => !['unknown','stale','unavailable','error'].includes(pool.state) && w?.present === true && typeof w.percent_remaining==='number' && Number.isFinite(w.percent_remaining)
        ? `${Math.round((100-w.percent_remaining)*10)/10}%` : 'Unknown';
      const label=el('th',displayAccount(pool.account));label.scope='row';tr.append(label,el('td',used(five)),el('td',used(week)));
      const reset=el('td');
      for (const [name,w] of [['5h',five],['Weekly',week]]) reset.append(el('div',`${name}: ${berlin(w?.reset_at)}`));
      tr.append(reset);
      const balances=pool.native_balance ? [pool.native_balance] : Object.values(windows)
        .filter(w=>['credits','usd_cents'].includes(w?.unit) && typeof w.credits_remaining==='number')
        .map(w=>({remaining:w.credits_remaining,unit:w.unit}));
      tr.append(el('td',balances.length ? [...new Set(balances.map(b=>b.unit==='usd_cents' ? `$${(b.remaining/100).toFixed(2)}` : `${b.remaining} credits`))].join('; ') : 'Unknown'));
      const cell=el('td');
      const grants=(banked?.grants || []).filter(g=>canonicalAccount(g.account)===canonicalAccount(pool.account) && g.active===1 && g.expires_at>now);
      if (!banked) cell.append(el('span','Unavailable'));
      else if (!grants.length) {
        const known=banked.observations?.some(o=>canonicalAccount(o.account)===canonicalAccount(pool.account) && now-(typeof o.observed_at==='number' ? o.observed_at : Date.parse(o.observed_at || ''))<20*60000);
        cell.append(el('span',known ? 'None reported' : 'Unknown'));
      }
      for(const grant of grants) {
        const soon=grant.expires_at-now<48*3600000;
        const item=el('div',`${grant.label} · expires ${berlin(grant.expires_at)}${soon ? ' · Expiring soon (<48h)' : ''}`,soon ? 'usage-expiring-soon' : '');
        item.append(el('div','Alexey approval required'));cell.append(item);
      }
      tr.append(cell);body.append(tr);
    }
    table.append(head,body);scroll.append(table);host.append(scroll);
  }

  async function load() {
    const current = ++generation;
    const note = root() ? root().querySelector('[data-usage-note]') : null;
    try {
      const [history, tokens, quota, banked] = await Promise.all([
        fetchApi(`/api/monitoring/history?range=${range}`),
        fetchApi('/api/monitoring/tokens').catch(() => null),
        fetchApi('/api/quota?view=fleet').catch(() => null),
        fetchApi('/api/quota/resets').catch(() => null),
      ]);
      if (current !== generation) return;
      accountAliases = quota?.canonical?.accounts || {};
      engineAliases = quota?.canonical?.engines || {};
      accountLabels = Object.fromEntries((quota?.pools || []).map(p => [p.account,
        `${(p.harness || p.engines || []).join(', ') || 'unknown'}/${p.provider} ${p.account}`]));
      if (note) note.textContent = statusLine(history);
      drawCurrent(quota, banked);
      drawQuota(history, '');
      drawTokens(history, tokens && tokens.sources);
    } catch {
      if (current !== generation) return;
      if (quotaRoot()) quotaRoot().replaceChildren(el('p', 'Quota history unavailable. Sign in or refresh to retry; measurements unknown.', 'metrics-chart-empty'));
      if (tokenRoot()) tokenRoot().replaceChildren(el('p', 'Token history unavailable. Sign in or refresh to retry; measurements unknown.', 'metrics-chart-empty'));
    }
  }

  window.HubUsageHistory = {
    refresh(api) {
      fetchApi = api;
      if (!document.getElementById('usage-quota-history')) return;
      if (!initialized) {
        initialized = true;
        const controls = el('div', '', 'metrics-project-filter');
        const label = el('label', 'Usage history range ');
        const select = el('select', '', 'dk-select');
        select.setAttribute('aria-label', 'Usage history range');
        for (const value of ['24h', '7d', '30d']) select.append(Object.assign(el('option', value), { value }));
        select.value = range;
        select.addEventListener('change', () => { range = select.value; load(); });
        label.append(select);
        controls.append(label);
        const host = root();
        const note = el('p', '', 'metrics-chart-note');
        note.setAttribute('data-usage-note', '');
        note.setAttribute('role', 'status');
        host.prepend(note, controls);
        matchMedia('(max-width: 600px)').addEventListener('change', () => load());
        setInterval(() => { if (!document.hidden && fetchApi) load(); }, 300000);
      }
      return load();
    },
    clear() {
      generation++;
      if (quotaRoot()) quotaRoot().replaceChildren();
      if (tokenRoot()) tokenRoot().replaceChildren();
      if (sourceRoot()) sourceRoot().replaceChildren();
      document.getElementById('usage-current')?.replaceChildren();
    },
  };
})();
