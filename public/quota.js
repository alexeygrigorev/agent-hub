(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, text, className = '') => Object.assign(document.createElement(tag), {
    textContent: text || '', className,
  });

  async function get(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.json();
  }

  function stateClass(state) {
    if (state === 'fresh') return 'quota-state--good';
    if (['stale', 'unavailable', 'exhausted'].includes(state)) return 'quota-state--bad';
    return 'quota-state--unknown';
  }

  function canonicalName(data, group, value) {
    return data.canonical?.[group]?.[value] || value || 'unknown';
  }

  function formatTime(value) {
    if (!value) return 'Unknown';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? 'Unknown' : date.toLocaleString();
  }

  function nativeHostReports(data, pool) {
    const reports = [];
    for (const host of data.hosts || []) {
      const snapshot = host.snapshot || {};
      if (snapshot.status !== 'ok') continue;
      const routeEntry = Object.entries(snapshot.routes || {}).find(([key]) =>
        canonicalName(data, 'providers', key) === pool.provider);
      if (!routeEntry || routeEntry[1]?.status !== 'ok') continue;
      for (const [windowName, window] of Object.entries(routeEntry[1].windows || {})) {
        const amount = window.credits_remaining != null
          ? `${window.credits_remaining} credits`
          : window.spend_used != null
            ? `spent ${window.spend_used}${window.unit && window.unit !== 'percent' ? ` ${window.unit}` : ''}`
            : null;
        if (amount) reports.push(`${host.host_id} ${windowName}: ${amount}`);
      }
    }
    return reports;
  }

  function showCoverage(data) {
    const machineCoverage = data.coverage || {};
    const accountCoverage = data.account_coverage || {};
    const coverageText = `Coverage: ${accountCoverage.fresh?.length || 0} fresh, ` +
      `${accountCoverage.stale?.length || 0} stale, ` +
      `${accountCoverage.unavailable?.length || 0} unknown or unavailable shared accounts ` +
      `(${accountCoverage.total ?? '?'} total); ` +
      `${machineCoverage.fresh?.length || 0}/${machineCoverage.machines_total ?? '?'} machines fresh.`;
    $('quota-coverage').textContent = coverageText;
    $('quota-status').textContent = `Authenticated · updated ${new Date().toLocaleTimeString()} · ` +
      `As of ${formatTime(data.as_of)}. ${coverageText}`;
  }

  function render(data, fleet) {
    const list = $('quota-list');
    const summary = $('quota-summary');
    list.replaceChildren();
    summary.replaceChildren();

    const pools = data.pools || [];
    const accounts = fleet.accounts || [];
    const byLabel = new Map(accounts.map(account => [
      canonicalName(data, 'accounts', account.label), account,
    ]));
    const counts = {
      pools: pools.length,
      fresh: pools.filter(pool => ['fresh', 'exhausted'].includes(pool.state)).length,
      stale: pools.filter(pool => pool.state === 'stale').length,
      unknown: pools.filter(pool => ['unknown', 'unavailable'].includes(pool.state)).length,
    };
    for (const [count, label] of [
      [counts.pools, 'Shared account pools'],
      [counts.fresh, 'Fresh observations'],
      [counts.stale, 'Stale'],
      [counts.unknown, 'Unknown / unavailable'],
    ]) {
      const card = document.createElement('div');
      card.append(element('strong', String(count)), element('span', label));
      summary.append(card);
    }

    if (!pools.length) {
      list.append(element('p',
        'No shared quota accounts are reported. Account and route setup is in Settings.',
        'quota-empty'));
      showCoverage(data);
      return;
    }

    for (const pool of pools) {
      const account = byLabel.get(canonicalName(data, 'accounts', pool.account));
      const state = pool.state || 'unknown';
      const card = document.createElement('article');
      card.className = 'quota-card';
      const heading = document.createElement('div');
      heading.className = 'quota-card__head';
      heading.append(
        element('h2', `${canonicalName(data, 'providers', pool.provider)} · ${canonicalName(data, 'accounts', pool.account)}`),
        element('span', state, `${stateClass(state)} quota-state`),
      );
      card.append(heading);

      const concurrency = account
        ? `${account.active ?? '?'} counted / ${account.max_concurrency ?? '?'} max` +
          `${account.placeable_slots == null ? '' : ` · ${account.placeable_slots} placeable`}` +
          `${account.reserved == null ? '' : ` · ${account.reserved} reserved`}`
        : 'Concurrency unknown';
      const remaining = pool.remaining_percent == null
        ? 'Unknown'
        : `${pool.remaining_percent}% remaining`;
      const resets = Object.values(pool.windows || {})
        .map(window => window.reset_at).filter(Boolean).sort();
      const forecast = account?.burn_forecast;
      const burn = forecast
        ? `~${Math.round(forecast.projected_unused_percent)}% unused at ${forecast.window} reset in ${Math.round(forecast.hours_to_reset)}h` +
          (forecast.burn_rate_percent_per_hour == null ? ' (burn rate unknown)' : forecast.waste_alert ? ' · likely to waste' : '')
        : 'No burn forecast';
      const details = document.createElement('div');
      details.className = 'quota-card__details';
      details.append(
        element('div', `${remaining} provider quota`),
        element('div', `${concurrency} concurrency`),
        element('div', burn),
        element('div', `Next reset ${formatTime(resets[0])}`),
      );
      card.append(details);

      const diagnostics = document.createElement('details');
      diagnostics.className = 'quota-card__diagnostics';
      diagnostics.append(element('summary', 'Provider windows and source details'));
      const source = pool.source === 'server'
        ? 'Hub provider probe'
        : pool.source === 'unmetered-engine' ? 'Unmetered engine' : 'Machine report';
      const nativeBalance = pool.native_balance
        ? `${pool.native_balance.remaining} ${pool.native_balance.unit === 'usd_cents' ? 'USD cents' : pool.native_balance.unit}`
        : 'not reported';
      const hostNative = nativeHostReports(data, pool);
      diagnostics.append(element('p',
        `Observed ${formatTime(pool.observed_at)} · Source: ${source} · Native balance ${nativeBalance} · ` +
        `Host native reports ${hostNative.join(', ') || 'not reported'} · ` +
        `Harness ${(pool.harness || pool.engines || []).map(engine => canonicalName(data, 'engines', engine)).join(', ') || 'unknown'} · ` +
        `Used by ${(pool.observed_by_hosts || []).join(', ') || 'no reporting machine'}`));

      const windows = document.createElement('div');
      windows.className = 'quota-card__windows';
      for (const [name, window] of Object.entries(pool.windows || {})) {
        const box = document.createElement('div');
        box.className = 'quota-window';
        const percent = window.present && window.percent_remaining != null
          ? `${window.percent_remaining}% remaining`
          : 'Unknown remaining';
        const native = window.credits_remaining != null
          ? `${window.credits_remaining} credits`
          : window.spend_used != null
            ? `Spent ${window.spend_used}${window.unit && window.unit !== 'percent' ? ` ${window.unit}` : ''}`
            : 'Native usage unknown';
        box.append(
          element('strong', name),
          element('span', percent, `${stateClass(state)} quota-state`),
          element('small', `${native} · Resets ${formatTime(window.reset_at)}`),
        );
        windows.append(box);
      }
      if (!windows.children.length) {
        windows.append(element('div', 'No provider window reported for this account.', 'quota-window'));
      }
      diagnostics.append(windows);

      const coverage = data.account_coverage || {};
      const freshness = coverage.stale?.includes(pool.account)
        ? 'stale observation'
        : coverage.unavailable?.includes(pool.account)
          ? 'unavailable observation'
          : state === 'fresh' ? 'current observation'
            : state === 'exhausted' ? 'current · exhausted' : 'unknown observation';
      diagnostics.append(element('p',
        `Freshness: ${freshness}. Provider quota and concurrency are separate limits.`));
      const resetLink = element('a', 'Review banked reset grants');
      resetLink.href = `/banked-resets.html?account=${encodeURIComponent(pool.account)}`;
      diagnostics.append(resetLink);
      card.append(diagnostics);
      list.append(card);
    }

    showCoverage(data);
  }

  async function refresh() {
    const status = $('quota-status');
    $('quota-refresh').disabled = true;
    status.textContent = 'Refreshing quota and concurrency…';
    try {
      const me = await get('/auth/me');
      for (const id of ['account-name', 'menu-name', 'menu-email']) $(id).textContent = me.email;
      document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0, 1).toUpperCase();
      const [quota, fleet] = await Promise.all([
        get('/api/quota?view=fleet'), get('/api/fleet'),
      ]);
      render(quota, fleet);
    } catch (error) {
      status.textContent = `Quota data unavailable. ${error.message}. Capacity remains unknown until a fresh source is available.`;
    } finally {
      $('quota-refresh').disabled = false;
    }
  }

  $('quota-refresh').addEventListener('click', refresh);
  $('sign-out').addEventListener('click', async () => {
    try {
      const me = await get('/auth/me');
      await fetch('/auth/logout', {
        method: 'POST', credentials: 'same-origin', redirect: 'manual',
        headers: { 'X-CSRF-Token': me.csrf_token },
      });
      location.assign('/auth/login');
    } catch {
      $('quota-status').textContent = 'Sign-out could not be confirmed. Try again.';
    }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
})();
