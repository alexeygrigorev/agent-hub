/* Quota-window and token usage history. No measurements are invented:
 * missing points stay gaps, unknown reset times draw no markers, and engines
 * without a native token log are labelled per engine with the reason.
 * Depends on HubMetricsCharts.timeSeries (metrics-charts.js). */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, text, cls) => Object.assign(document.createElement(tag), { textContent: text || '', className: cls || '' });
  const RESET_SUFFIX = '__reset_at';
  // Display-only harness/provider mapping (mirrors src/account-display.ts;
  // stored history keys never change, so old rows join renamed accounts).
  // History entities are account labels, so map labels (and provider keys).
  const DISPLAY = {
    codex: ['codex/openai', 'shared-codex'],
    'codex-orin': ['codex/openai', 'orin'],
    zai: ['codex/zai', 'shared-zai'],
    go: ['opencode/go', 'opencode-go'],
    'shared-codex': ['codex/openai', 'shared-codex'],
    'shared-zai': ['codex/zai', 'shared-zai'],
    'urgent-muse': ['opencode/go', 'opencode-go'],
  };
  const ALIAS = { 'codex/orin': 'codex-orin', 'opencode-go': 'go', 'codex/zai': 'zai' };
  const displayAccount = label => {
    const key = ALIAS[label] || label;
    const known = DISPLAY[key];
    if (known) return `${known[0]} ${known[1]}`;
    return label;
  };
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
    const marks = [];
    let previous = null;
    for (const p of resets) {
      if (typeof p.value !== 'number' || !Number.isFinite(p.value)) continue;
      if (previous !== null && p.value !== previous) marks.push(p.at);
      previous = p.value;
    }
    return marks;
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
      title.textContent = `Window reset at ${new Date(at).toISOString()}`;
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
      if (!byAccount.has(s.entity)) byAccount.set(s.entity, []);
      byAccount.get(s.entity).push(s);
    }
    if (!byAccount.size) {
      hostDiv.append(el('p', 'No per-window quota history in this range yet. History accumulates from every quota collection; null stays a gap.', 'metrics-chart-empty'));
      return;
    }
    for (const [account, rows] of byAccount) {
      const lines = rows.filter(s => !s.metric.endsWith(RESET_SUFFIX));
      // Windows with no known points leave no trace: an empty/null line is
      // not a chart (AC2 renders with real data, never only a null line).
      const real = lines.filter(s => s.points.some(p => typeof p.value === 'number' && Number.isFinite(p.value)));
      if (!real.length) continue;
      const resetsByWindow = new Map();
      for (const s of rows.filter(s => s.metric.endsWith(RESET_SUFFIX))) {
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
        figure.append(el('p', `${marks} window reset marker${marks === 1 ? '' : 's'} (vertical line) — a new window began; values before and after are not comparable.`, 'metrics-chart-note'));
      }
      hostDiv.append(figure);
    }
    hostDiv.append(el('p', 'One line per window (5h / 7d / monthly where the provider reports it). ' +
      'Reset markers come from the provider-reported reset time; unknown resets draw no markers.', 'metrics-chart-note'));
  }

  function drawTokens(data, sources) {
    const host = tokenRoot();
    host.replaceChildren();
    const start = Date.parse(data.start), end = Date.parse(data.end);
    const tokens = (data.series || []).filter(s => s.kind === 'tokens');
    const byEntity = new Map();
    for (const s of tokens) {
      const key = `${s.machine_id || ''}\0${s.entity}`;
      if (!byEntity.has(key)) byEntity.set(key, { entity: s.entity, machine: s.machine_id, metrics: new Map() });
      byEntity.get(key).metrics.set(s.metric, s.points);
    }
    if (!byEntity.size) {
      host.append(el('p', 'No native token history in this range yet. Hourly collection posts complete hours only; absence is unknown, never zero.', 'metrics-chart-empty'));
    }
    for (const { entity, metrics } of byEntity.values()) {
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

  async function load() {
    const current = ++generation;
    const note = root() ? root().querySelector('[data-usage-note]') : null;
    try {
      const [history, tokens] = await Promise.all([
        fetchApi(`/api/monitoring/history?range=${range}`),
        fetchApi('/api/monitoring/tokens').catch(() => null),
      ]);
      if (current !== generation) return;
      if (note) note.textContent = statusLine(history);
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
    },
  };
})();
