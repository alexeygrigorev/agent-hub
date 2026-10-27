/* TBM dashboard: spend-vs-pace per pool, banked deadlines, kill-switch.
 * Quota-percent only; real token consumption is unknown and never inferred.
 * Same auth/CSRF contract as metrics.js: same-origin, X-CSRF-Token, 401 -> login. */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const byId = (id) => document.getElementById(id);
  const el = (tag, text, cls) => Object.assign(document.createElement(tag), { textContent: text == null ? '' : String(text), className: cls || '' });
  const shown = (v) => (v === null || v === undefined ? 'unknown' : String(v));
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const percentPool = (pool) => pool.capacity_known === 0 || pool.quota_unit === 'percent';
  const poolAmounts = (pool) => percentPool(pool)
    ? { total: 100, remaining: num(pool.remaining_percent) ?? num(pool.headroom_percentage) }
    : { total: num(pool.total_capacity_tokens), remaining: num(pool.remaining_tokens) };
  const fmtInt = (v) => (v === null ? 'unknown' : new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(v));
  const utc = (ms) => {
    const t = num(ms);
    if (t === null) return 'unknown';
    try { return new Date(t).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'; } catch { return 'unknown'; }
  };
  const countdown = (ms) => {
    if (ms === null || !Number.isFinite(ms)) return 'unknown';
    if (ms <= 0) return 'reset due';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  };

  let csrf = null;

  async function apiGet(path) {
    const headers = {};
    if (csrf) headers['X-CSRF-Token'] = csrf;
    const r = await fetch(path, { method: 'GET', headers, credentials: 'same-origin', cache: 'no-store' });
    if (r.status === 401) { const e = Error('expired'); e.code = 'expired'; throw e; }
    if (r.status === 403) { const e = Error('access_denied'); e.code = 'access_denied'; throw e; }
    if (!r.ok) { const e = Error('service_failure'); e.code = 'service_failure'; throw e; }
    return r.json();
  }

  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
      body: JSON.stringify(body),
    });
    if (r.status === 401) { const e = Error('expired'); e.code = 'expired'; throw e; }
    if (r.status === 403) { const e = Error('access_denied'); e.code = 'access_denied'; throw e; }
    if (!r.ok) { const e = Error('service_failure'); e.code = 'service_failure'; throw e; }
    return r.json();
  }

  function svgEl(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // Projected remaining-quota lines from now to reset at target vs current burn.
  // Pure projection of quota units; not measured token spend.
  function paceChart(pool, nowMs) {
    const fig = el('figure', '', 'metrics-chart');
    fig.append(el('h3', `Spend vs pace to reset (${percentPool(pool) ? 'quota-percent' : 'token'} projection)`));
    const { total, remaining } = poolAmounts(pool);
    const reset = num(pool.reset_at_ms);
    const target = num(pool.target_burn_rate_tpm);
    const current = num(pool.current_burn_rate_tpm);
    if (total === null || remaining === null || reset === null || !(reset > nowMs)) {
      fig.append(el('p', 'Unknown — pool needs total, remaining and a future reset to project pace.', 'metrics-chart-empty'));
      return fig;
    }
    const W = 760; const H = 220; const L = 56; const R = 16; const T = 14; const B = 44;
    const minutesLeft = Math.max(1, (reset - nowMs) / 60000);
    const atTarget = target !== null ? Math.max(0, remaining - target * minutesLeft) : null;
    const atCurrent = current !== null ? Math.max(0, remaining - current * minutesLeft) : null;
    const hard = num(pool.hard_floor_percent) !== null ? (total * num(pool.hard_floor_percent)) / 100 : null;
    const softFrac = num(pool.soft_reserve_percent);
    const soft = softFrac !== null ? (total * softFrac) / 100 : null;
    const top = Math.max(total, remaining, hard || 0, soft || 0, 1);
    const x0 = L; const x1 = W - R;
    const y = (v) => T + (1 - v / top) * (H - T - B);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Projected quota for ${pool.pool_id || pool.provider || 'pool'} to reset` });
    for (const frac of [0, 0.5, 1]) {
      const v = frac * top;
      svg.append(svgEl('line', { x1: x0, x2: x1, y1: y(v), y2: y(v), class: 'metrics-grid' }));
      svg.append(svgEl('text', { x: x0 - 8, y: y(v) + 4, 'text-anchor': 'end', class: 'metrics-axis' },
        new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(v)));
    }
    const line = (vEnd, cls, label) => {
      if (vEnd === null) return;
      const g = svgEl('g', { class: cls });
      g.append(svgEl('path', { d: `M${x0},${y(remaining)} L${x1},${y(vEnd)}`, class: 'metrics-line', 'aria-hidden': 'true' }));
      const dot = svgEl('circle', { cx: x1, cy: y(vEnd), r: 4, class: 'metrics-point', tabindex: 0, role: 'img', 'aria-label': label });
      dot.append(svgEl('title', {}, label));
      g.append(dot);
      svg.append(g);
    };
    if (hard !== null) svg.append(svgEl('line', { x1: x0, x2: x1, y1: y(hard), y2: y(hard), class: 'metrics-grid' }));
    line(atTarget, 'metrics-series metrics-series-0', `At target burn: ${fmtInt(atTarget)} quota units left at reset`);
    line(atCurrent, 'metrics-series metrics-series-1', `At current burn: ${fmtInt(atCurrent)} quota units left at reset`);
    svg.append(svgEl('text', { x: x0, y: H - 22, class: 'metrics-axis' }, 'now'));
    svg.append(svgEl('text', { x: x1, y: H - 22, 'text-anchor': 'end', class: 'metrics-axis' }, 'reset'));
    svg.append(svgEl('text', { x: x1, y: H - 4, 'text-anchor': 'end', class: 'metrics-axis' }, utc(reset)));
    fig.append(svg);
    const legend = el('ul', '', 'metrics-legend');
    if (target !== null) {
      const li = el('li', '', 'metrics-series metrics-series-0');
      li.append(el('span', '', 'metrics-swatch'), el('span', `Target ${fmtInt(target)} units/min`));
      legend.append(li);
    }
    if (current !== null) {
      const li = el('li', '', 'metrics-series metrics-series-1');
      li.append(el('span', '', 'metrics-swatch'), el('span', `Current ${fmtInt(current)} units/min`));
      legend.append(li);
    }
    if (hard !== null) legend.append(el('li', `Hard floor ${fmtInt(hard)} units`));
    fig.append(legend);
    fig.append(el('p', 'Projection of quota units only; real token consumption is unknown.', 'metrics-chart-note'));
    return fig;
  }

  function poolCard(pool, nowMs, routingFactors) {
    const card = el('div', '', 'tbm-pool');
    const provider = pool.provider || 'unknown';
    const account = pool.account || 'default';
    const title = `${provider} · ${account} · ${pool.pool_id || 'pool'}`;
    card.append(el('h3', title));
    const { total, remaining } = poolAmounts(pool);
    const pct = (total !== null && remaining !== null && total > 0)
      ? `${((remaining / total) * 100).toFixed(1)}% quota left` : 'quota percent unknown';
    const reset = num(pool.reset_at_ms);
    const meta = el('p', '', 'tbm-pool-meta');
    meta.textContent =
      `${pct} · remaining ${fmtInt(remaining)} / ${fmtInt(total)} quota units` +
      ` · headroom ${shown(pool.headroom_percentage)}%` +
      ` · reset ${utc(reset)} (${countdown(reset !== null ? reset - nowMs : null)})` +
      ` · window ${shown(pool.window_type)} · cycle ${shown(pool.cycle_ms)} ms · unit ${shown(pool.quota_unit)}` +
      ` · pacing ${shown(pool.pacing_ratio)} · target ${shown(pool.target_burn_rate_tpm)} · current ${shown(pool.current_burn_rate_tpm)} units/min` +
      ` · kill-switch ${pool.kill_switch_active ? 'ACTIVE' : 'off'}`;
    card.append(meta);
    card.append(paceChart(pool, nowMs));
    const key = provider;
    if (routingFactors && key && Object.prototype.hasOwnProperty.call(routingFactors, key)) {
      const f = el('p', '', 'tbm-factors');
      f.textContent = `Routing factors for ${key}: ${JSON.stringify(routingFactors[key])}`;
      card.append(f);
    }
    if (pool.kill_switch_active) {
      const warn = el('p', `Kill-switch ACTIVE${pool.kill_switch_reason ? `: ${pool.kill_switch_reason}` : ''}`, 'tbm-status-active');
      card.append(warn);
    }
    return card;
  }

  function renderPacing(data, routingFactors) {
    const root = byId('tbm-pacing');
    if (!root) return;
    root.replaceChildren();
    const nowMs = Date.parse(data.as_of) || Date.now();
    const pools = Array.isArray(data.pools) ? data.pools : [];
    if (!pools.length) {
      root.append(el('p', 'No quota pools reported. Unknown is not zero.', 'metrics-chart-empty'));
      return;
    }
    root.append(el('p', `As of ${shown(data.as_of)}. Percent pools use percentage points/minute; real token capacity is unknown for those pools.`, 'tbm-note'));
    for (const pool of pools) root.append(poolCard(pool || {}, nowMs, routingFactors));
  }

  function renderBanked(data) {
    const root = byId('tbm-banked');
    if (!root) return;
    root.replaceChildren();
    const nowMs = Date.parse(data.as_of) || Date.now();
    const tranches = (Array.isArray(data.tranches) ? data.tranches.slice() : [])
      .sort((a, b) => (num(a.expires_at_ms) || 0) - (num(b.expires_at_ms) || 0));
    if (!tranches.length) {
      root.append(el('p', 'No banked quota. Unknown is not zero.', 'metrics-chart-empty'));
      return;
    }
    const table = document.createElement('table');
    table.className = 'dk-table';
    const head = document.createElement('tr');
    for (const t of ['Tranche', 'Pool', 'Remaining / allocated', 'Expires', 'Time left', 'Status']) {
      const th = document.createElement('th'); th.scope = 'col'; th.textContent = t; head.append(th);
    }
    const thead = document.createElement('thead'); thead.append(head); table.append(thead);
    const body = document.createElement('tbody');
    for (const tr of tranches) {
      const row = document.createElement('tr');
      const exp = num(tr.expires_at_ms);
      const cells = [
        tr.label || tr.tranche_id || 'tranche',
        tr.pool_id || `${tr.provider || 'unknown'}`,
        tr.quantity_known === 0 ? 'Full reset available; token quantity unknown' : `${fmtInt(num(tr.remaining_tokens))} / ${fmtInt(num(tr.allocated_tokens))} tokens`,
        utc(exp),
        countdown(exp !== null ? exp - nowMs : null),
        shown(tr.status),
      ];
      cells.forEach((text, i) => {
        const td = document.createElement('td');
        td.dataset.label = ['Tranche', 'Pool', 'Remaining / allocated', 'Expires', 'Time left', 'Status'][i];
        td.textContent = text;
        row.append(td);
      });
      body.append(row);
    }
    table.append(body);
    const wrap = el('div', '', 'scroll');
    wrap.append(table);
    root.append(wrap);
    root.append(el('p', 'Earliest deadline first. Real token value of banked quota is unknown.', 'tbm-note'));
  }

  function renderKill(data) {
    const root = byId('tbm-kill');
    if (!root) return;
    root.replaceChildren();
    const active = data.active === true;
    const status = el('p', '', active ? 'tbm-status-active' : 'tbm-status-idle');
    status.id = 'tbm-kill-status';
    status.textContent = active
      ? `Kill-switch ACTIVE${data.reason ? `: ${data.reason}` : ''}`
      : 'Kill-switch off. Autonomous burn allowed unless floors trigger.';
    const form = document.createElement('form');
    form.id = 'tbm-kill-form';
    form.className = 'tbm-kill';
    const reasonWrap = el('div', '', 'dk-field');
    const label = el('label', 'Reason (required to change state)');
    label.setAttribute('for', 'tbm-kill-reason');
    label.className = 'dk-label';
    const input = document.createElement('input');
    input.id = 'tbm-kill-reason';
    input.name = 'reason';
    input.type = 'text';
    input.className = 'dk-input';
    input.maxLength = 280;
    input.autocomplete = 'off';
    input.placeholder = 'e.g. staged low-quota drill';
    input.value = typeof data.reason === 'string' ? data.reason : '';
    reasonWrap.append(label, input);
    const btn = document.createElement('button');
    btn.type = 'submit';
    btn.id = 'tbm-kill-toggle';
    btn.className = 'dk-button ' + (active ? 'dk-button--secondary' : 'dk-button--danger');
    btn.textContent = active ? 'Release kill-switch' : 'Activate kill-switch';
    form.append(reasonWrap, btn);
    const msg = el('p', '', 'tbm-note');
    msg.id = 'tbm-kill-message';
    msg.setAttribute('role', 'status');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      msg.textContent = 'Saving…';
      btn.disabled = true;
      try {
        const reason = input.value.trim();
        if (!reason) { msg.textContent = 'A reason is required.'; return; }
        const next = await apiPost('/api/quota/kill-switch', { active: !active, reason });
        renderKill(next || { active: !active, reason });
      } catch (e) {
        if (e && e.code === 'expired') { location.assign('/auth/login'); return; }
        msg.textContent = e && e.code === 'access_denied' ? 'Access denied.' : 'Kill-switch update failed.';
      } finally {
        btn.disabled = false;
      }
    });
    root.append(status, form, msg);
  }

  function renderEmpty(reason) {
    for (const id of ['tbm-pacing', 'tbm-banked', 'tbm-kill']) {
      const n = byId(id);
      if (n) n.replaceChildren(el('p', reason, 'metrics-chart-empty'));
    }
  }

  async function renderGrok() {
    const root = byId('tbm-grok');
    if (!root) return;
    root.replaceChildren();
    try {
      const policy = await apiGet('/api/fleet/routing/policy/grok');
      root.append(el('p', `Saved preference: ${policy.mode}. Effective state: disabled. Grok is reserved for bots and unavailable for all Hub tasks.`));
      const form = el('form');
      const label = el('label', 'Saved Grok preference (cannot enable routing)');
      const select = el('select', null, 'dk-select'); select.id = 'tbm-grok-mode'; label.htmlFor = select.id;
      for (const mode of ['minimal', 'disabled', 'normal']) {
        const option = el('option', mode); option.value = mode; option.selected = policy.mode === mode; select.append(option);
      }
      const reasonLabel = el('label', 'Reason for change');
      const reason = el('input', null, 'dk-input'); reason.id = 'tbm-grok-reason'; reasonLabel.htmlFor = reason.id;
      reason.required = true; reason.maxLength = 500;
      const save = el('button', 'Save Grok preference', 'dk-button'); save.type = 'submit';
      const status = el('p'); status.setAttribute('role', 'status');
      form.append(label, select, reasonLabel, reason, save, status);
      form.addEventListener('submit', async (event) => {
        event.preventDefault(); save.disabled = true;
        try {
          await apiPost('/api/fleet/routing/policy/grok', { mode: select.value, reason: reason.value });
          await renderGrok();
        } catch (e) {
          status.textContent = e.code === 'access_denied' ? 'Only an operator can change Grok preference.' : 'Could not save Grok preference.';
          save.disabled = false;
        }
      });
      root.append(form);
    } catch (e) {
      root.append(el('p', e.code === 'access_denied' ? 'Grok policy access denied.' : 'Grok policy unavailable.'));
    }
  }

  async function refresh() {
    const status = byId('tbm-status');
    try {
      const me = await apiGet('/auth/me');
      csrf = me.csrf_token || null;
      const user = me.email || 'authenticated';
      const [pacing, banked, kill] = await Promise.all([
        apiGet('/api/quota/pacing').catch((e) => { throw e; }),
        apiGet('/api/quota/banked').catch((e) => { throw e; }),
        apiGet('/api/quota/kill-switch').catch((e) => { throw e; }),
      ]);
      renderPacing(pacing, pacing.routing_factors);
      renderBanked(banked);
      renderKill(kill);
      await renderGrok();
      if (status) status.textContent = `TBM · ${user} · as of ${pacing.as_of || banked.as_of || 'unknown'}`;
    } catch (e) {
      if (e && e.code === 'expired') { location.assign('/auth/login'); return; }
      if (status) status.textContent = e && e.code === 'access_denied' ? 'TBM · access denied.' : 'TBM data unavailable.';
      if (e && e.code !== 'access_denied') renderEmpty('TBM data unavailable. Unknown is not zero.');
    }
  }

  function init() {
    if (!byId('tbm-pacing') && !byId('tbm-banked') && !byId('tbm-kill')) return;
    const btn = byId('tbm-refresh');
    if (btn) btn.addEventListener('click', () => { btn.disabled = true; refresh().finally(() => { btn.disabled = false; }); });
    refresh();
  }

  window.HubTbm = { refresh, renderPacing, renderBanked, renderKill };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
