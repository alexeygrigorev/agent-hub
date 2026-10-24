/* Dependency-free charts of sanitized metrics. Missing measurements never become zero. */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const HOUR = 3600000;
  const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const fmt = value => new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
  const compact = value => new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  const utc = value => new Date(value).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
  const el = (tag, text, className) => Object.assign(document.createElement(tag), { textContent: text || '', className: className || '' });
  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const target = id => document.getElementById(id);
  let selectedProject = '*';
  let activityData = null;
  const mobile = matchMedia('(max-width: 600px)');
  mobile.addEventListener('change', () => { if (activityData) activity(activityData); });

  function coverage(data, surface) {
    const code = data.coverage?.[surface] || 'unknown';
    return `Coverage: ${code}${code === 'partial' ? ' — observed portion only; missing work is unknown' : ''}${data.stale || data.status === 'stale' ? ' · stale snapshot' : ''}.`;
  }

  function lineChart(title, series, start) {
    const figure = el('figure', '', 'metrics-chart');
    figure.append(el('h3', title));
    const values = series.flatMap(s => s.values.filter(number));
    const counts = el('ul', '', 'metrics-legend');
    for (const s of series) {
      const item = el('li', '', `metrics-series metrics-series-${s.index % 5}`);
      item.append(el('span', '', 'metrics-swatch'), el('span', `${s.project} · ${s.values.filter(number).length}/24 known${s.partial ? ' · partial' : ''}`));
      counts.append(item);
    }
    figure.append(counts);
    if (!values.length) {
      figure.append(el('p', 'Unknown — no proven hourly measurements for this selection.', 'metrics-chart-empty'));
      return figure;
    }
    // The shared UTC grid prevents a missing bucket from shifting later points.
    const W = mobile.matches ? 340 : 760, H = 250, L = 46, R = 26, T = 18, B = 48;
    const max = Math.max(...values);
    const scale = max || 1; // Geometry only; an all-zero axis still labels only proven zero.
    const x = i => L + (i + 0.5) * (W - L - R) / 24;
    const y = v => T + (1 - v / scale) * (H - T - B);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'group', 'aria-label': `${title}, hourly UTC by project. Focus a point for its exact value.` });
    const info = el('figcaption', 'Hover or focus a point for the exact UTC interval and value.', 'metrics-point-detail');
    info.setAttribute('aria-live', 'polite');
    for (const ratio of max ? [0, 0.5, 1] : [0]) {
      const v = ratio * scale;
      svg.append(svgEl('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: 'metrics-grid' }));
      svg.append(svgEl('text', { x: L - 9, y: y(v) + 4, 'text-anchor': 'end', class: 'metrics-axis' }, compact(v)));
    }
    for (const i of [0, 6, 12, 18, 23]) {
      const time = new Date(start + i * HOUR).toISOString().slice(11, 16);
      svg.append(svgEl('text', { x: x(i), y: H - 22, 'text-anchor': 'middle', class: 'metrics-axis' }, time));
    }
    svg.append(svgEl('text', { x: W - R, y: H - 3, 'text-anchor': 'end', class: 'metrics-axis' }, 'Hour start · UTC'));
    for (const s of series) {
      const group = svgEl('g', { class: `metrics-series metrics-series-${s.index % 5}`, 'data-project': s.project });
      let path = '';
      let connected = false;
      s.values.forEach((value, i) => {
        if (!number(value)) { connected = false; return; }
        path += `${connected ? 'L' : 'M'}${x(i)},${y(value)} `;
        connected = true;
      });
      group.append(svgEl('path', { d: path, class: 'metrics-line', 'aria-hidden': 'true' }));
      s.values.forEach((value, i) => {
        if (!number(value)) return;
        const description = `${s.project} · ${utc(start + i * HOUR)} to ${utc(start + (i + 1) * HOUR)} · ${title}: ${fmt(value)}${s.partial ? ' · partial coverage' : ''}`;
        const dot = svgEl('circle', { cx: x(i), cy: y(value), r: 4, tabindex: 0, role: 'img', 'aria-label': description, 'data-value': value, 'data-hour': i, class: 'metrics-point' });
        dot.append(svgEl('title', {}, description));
        const show = () => { info.textContent = description; };
        dot.addEventListener('focus', show);
        dot.addEventListener('pointerenter', show);
        group.append(dot);
      });
      svg.append(group);
    }
    figure.append(svg, info);
    return figure;
  }

  function activity(data) {
    activityData = data;
    const root = target('activity-charts');
    root.replaceChildren();
    const projects = Object.entries(data.utilization?.projects || {});
    const note = el('p', coverage(data, 'utilization'), 'metrics-chart-note');
    root.append(note);
    const asOf = Date.parse(data.as_of);
    if (!Number.isFinite(asOf)) {
      root.append(el('p', 'Unknown — no valid 24-hour window.', 'metrics-chart-empty'));
      return;
    }
    const start = asOf - 24 * HOUR;
    root.append(el('p', `${utc(start)} → ${utc(asOf)} (end excluded)`, 'metrics-window'));
    if (!projects.length) {
      root.append(el('p', 'Unknown — no projects reported.', 'metrics-chart-empty'));
      return;
    }
    if (selectedProject !== '*' && !projects.some(([p]) => p === selectedProject)) selectedProject = '*';
    const label = el('label', 'Project ', 'metrics-project-filter');
    const select = el('select', '', 'dk-select');
    select.setAttribute('aria-label', 'Chart project');
    select.append(Object.assign(el('option', 'All projects'), { value: '*' }));
    for (const [p] of projects) select.append(Object.assign(el('option', p), { value: p }));
    select.value = selectedProject;
    select.addEventListener('change', () => {
      selectedProject = select.value;
      draw();
    });
    label.append(select);
    const plots = el('div', '', 'metrics-plots');
    root.append(label, plots);
    function draw() {
      plots.replaceChildren();
      for (const [title, field] of [['Active agents', 'active_agents'], ['Agent hours', 'agent_hours']]) {
        const series = projects.map(([project, body], index) => {
          const buckets = new Map((body?.hourly_buckets || []).map(b => [Date.parse(b.bucket_start), b]));
          return {
            project, index,
            partial: data.coverage?.utilization === 'partial' || (number(body?.coverage) && body.coverage < 1),
            values: Array.from({ length: 24 }, (_, i) => body?.unknown === false && data.utilization?.unknown !== true ? buckets.get(start + i * HOUR)?.[field] : null),
          };
        }).filter(s => selectedProject === '*' || s.project === selectedProject);
        plots.append(lineChart(title, series, start));
      }
    }
    draw();
  }

  function bars(data, surface, id, fields) {
    const root = target(id);
    root.replaceChildren(el('p', coverage(data, surface), 'metrics-chart-note'));
    const body = data[surface] || {};
    const projects = Object.entries(body.projects || {});
    // An aggregate is useful when no per-project outcome breakdown exists.
    if (surface === 'accepted' && !projects.length) projects.push(['All projects', body]);
    const values = projects.flatMap(([, p]) => fields.map(([, f]) => p?.unknown === false && body.unknown !== true ? p[f] : null)).filter(number);
    const max = Math.max(1, ...values);
    root.append(el('p', '24-hour window totals by project. Hourly history is not supplied for this metric.', 'metrics-chart-note'));
    if (!projects.length) root.append(el('p', 'Unknown — no project measurements reported.', 'metrics-chart-empty'));
    for (const [project, p] of projects) {
      const group = el('div', '', 'metrics-bar-group');
      group.append(el('h3', project));
      if (p?.has_unknown_tokens) group.append(el('p', 'Partial token coverage — reported counts only; additional usage is unknown.', 'metrics-chart-note'));
      fields.forEach(([label, field], index) => {
        const value = p?.unknown === false && body.unknown !== true ? p[field] : null;
        const row = el('div', '', `metrics-bar-row metrics-series metrics-series-${index}`);
        row.append(el('span', label, 'metrics-bar-label'));
        const track = el('span', '', 'metrics-bar-track');
        track.setAttribute('aria-hidden', 'true');
        if (number(value)) {
          const bar = el('span', '', 'metrics-bar');
          bar.style.width = `${value / max * 100}%`;
          track.append(bar);
          row.dataset.value = String(value);
        } else track.classList.add('metrics-bar-unknown');
        row.append(track, el('span', number(value) ? fmt(value) : 'Unknown', 'metrics-bar-value'));
        group.append(row);
      });
      root.append(group);
    }
  }

  // Shared SVG renderer for timestamped host/account measurements. Uses the
  // same geometry, palette, legends and accessible points as hourly activity.
  function timeSeries(title, series, start, end, unit = '') {
    const figure = el('figure', '', 'metrics-chart');
    figure.append(el('h3', title));
    const legend = el('ul', '', 'metrics-legend');
    series.forEach((s, i) => {
      const item = el('li', '', `metrics-series metrics-series-${i % 5}`);
      const known = s.points.filter(p => number(p.value));
      item.append(el('span', '', 'metrics-swatch'), el('span', `${s.label}${s.reference ? ' · reference threshold' : known.length ? ` · latest ${fmt(known.at(-1).value)}${unit}` : ' · unknown'}`));
      legend.append(item);
    });
    figure.append(legend);
    const values = series.flatMap(s => s.points.map(p => p.value).filter(number));
    if (!values.length) { figure.append(el('p', 'No measurements in this range. Gaps mean unknown.', 'metrics-chart-empty')); return figure; }
    const W = mobile.matches ? 340 : 760, H = 250, L = 46, R = 26, T = 18, B = 48;
    const max = Math.max(...values), scale = max || 1;
    const x = at => L + (at - start) / (end - start) * (W - L - R);
    const y = v => T + (1 - v / scale) * (H - T - B);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'group', 'aria-label': `${title}. Touch or focus a point for its observation time and value.` });
    const info = el('figcaption', 'Touch, hover or focus a point for the measured time and value.', 'metrics-point-detail');
    info.setAttribute('aria-live', 'polite');
    for (const ratio of max ? [0, 0.5, 1] : [0]) {
      const v = ratio * scale;
      svg.append(svgEl('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: 'metrics-grid' }));
      svg.append(svgEl('text', { x: L - 9, y: y(v) + 4, 'text-anchor': 'end', class: 'metrics-axis' }, compact(v)));
    }
    for (const ratio of [0, 0.5, 1]) {
      const at = start + (end - start) * ratio;
      const label = new Date(at).toISOString().slice(end - start > 86400000 ? 5 : 11, end - start > 86400000 ? 10 : 16);
      svg.append(svgEl('text', { x: x(at), y: H - 22, 'text-anchor': ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle', class: 'metrics-axis' }, label));
    }
    svg.append(svgEl('text', { x: W - R, y: H - 3, 'text-anchor': 'end', class: 'metrics-axis' }, `UTC · ${unit || 'count'}`));
    series.forEach((s, i) => {
      const group = svgEl('g', { class: `metrics-series metrics-series-${i % 5}`, 'data-series': s.label });
      let path = '', connected = false;
      for (const p of s.points) {
        if (!number(p.value)) { connected = false; continue; }
        path += `${connected ? 'L' : 'M'}${x(p.at)},${y(p.value)} `;
        connected = true;
      }
      group.append(svgEl('path', { d: path, class: 'metrics-line', 'aria-hidden': 'true' }));
      for (const p of s.points) {
        if (!number(p.value)) continue;
        const description = `${s.label} · ${utc(p.at)} · ${fmt(p.value)}${unit}${s.reference ? ' · reference threshold' : ''}`;
        const dot = svgEl('circle', { cx: x(p.at), cy: y(p.value), r: 3, tabindex: 0, role: 'img', 'aria-label': description, 'data-value': p.value, class: 'metrics-point' });
        dot.append(svgEl('title', {}, description));
        const show = () => { info.textContent = description; };
        dot.addEventListener('focus', show);
        dot.addEventListener('pointerenter', show);
        dot.addEventListener('pointerdown', show);
        group.append(dot);
      }
      svg.append(group);
    });
    // Dense minute histories need a usable touch target: touching anywhere
    // in the plot reports the nearest bucket, including explicit unknowns.
    svg.addEventListener('pointerdown', event => {
      const bounds = svg.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, ((event.clientX - bounds.left) / bounds.width * W - L) / (W - L - R)));
      const at = start + fraction * (end - start);
      const details = series.map(s => {
        const p = s.points.reduce((best, p) => !best || Math.abs(p.at - at) < Math.abs(best.at - at) ? p : best, null);
        return `${s.label}: ${p && number(p.value) ? fmt(p.value) + unit + (s.reference ? ' (reference threshold)' : ' at ' + utc(p.at)) : 'unknown (gap)'}`;
      });
      info.textContent = `Nearest observations · ${utc(at)} · ${details.join(' · ')}`;
    });
    figure.append(svg, info);
    return figure;
  }

  window.HubMetricsCharts = {
    timeSeries,
    render(data) {
      activity(data);
      bars(data, 'usage', 'usage-chart', [['Input', 'total_input_tokens'], ['Output', 'total_output_tokens']]);
      bars(data, 'accepted', 'accepted-chart', [['Accepted', 'accepted_outcomes']]);
    },
    clear(reason) {
      activityData = null;
      for (const id of ['activity-charts', 'usage-chart', 'accepted-chart']) target(id).replaceChildren(el('p', reason + ' Measurements unknown.', 'metrics-chart-empty'));
    },
  };
})();
