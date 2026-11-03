(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, text, className = '') => Object.assign(document.createElement(tag), {
    textContent: text || '', className,
  });
  const WINDOW_LABELS = { '5h': '5h', five_hour: '5h', '7d': '7d', weekly: '7d', monthly: 'monthly' };
  const WINDOW_ORDER = ['5h', '7d', 'monthly'];
  const PROVIDER_TITLES = {
    claude: 'Claude',
    codex: 'Codex',
    copilot: 'Copilot',
    gemini: 'Gemini',
    go: 'Go',
    grok: 'Grok',
    zai: 'Zai',
    antigravity: 'Antigravity',
    opencode: 'OpenCode',
  };

  async function get(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 401) {
      location.assign('/auth/login');
      throw new Error('Sign-in required');
    }
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

  function displayProvider(raw) {
    const lower = String(raw || '').toLowerCase();
    return PROVIDER_TITLES[lower] || raw || 'Unknown provider';
  }

  function formatTime(value) {
    if (!value) return 'Unknown';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? 'Unknown' : date.toLocaleString();
  }

  function percent(value) {
    return Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value) : null;
  }

  function resetText(value, remaining = null) {
    const ts = typeof value === 'number' ? value : Date.parse(value || '');
    if (!Number.isFinite(ts)) return remaining === 100 ? '' : 'reset time unknown';
    const ms = ts - Date.now();
    if (ms <= 0) return 'reset passed · waiting for a fresh observation';
    const totalMinutes = Math.floor(ms / 60000);
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    if (days > 0) return `in ${days}d ${hours}h`;
    if (hours > 0) return `in ${hours}h ${minutes}m`;
    return `in ${Math.max(1, minutes)}m`;
  }

  function formatProviderError(pool, state, cred) {
    if (typeof pool.error === 'string' && pool.error && pool.error !== 'unavailable') {
      return pool.error;
    }
    const rawReason = pool.status_reason ||
      (cred && (cred.status === 'invalid' || cred.status_reason) ? (cred.status_reason || cred.status) : null);
    if (typeof rawReason === 'string' && rawReason) {
      const reason = rawReason.replace(/^rejected_once:/, '');
      if (reason === 'quota_unauthorized' || reason === 'quota_http_401') {
        return 'HTTP Error 401: Unauthorized';
      }
      if (reason.startsWith('quota_http_')) {
        return `HTTP Error ${reason.slice('quota_http_'.length)}`;
      }
      if (reason === 'credential_missing') return 'Credential missing for this account.';
      if (reason === 'quota_blocked') return 'Provider quota request blocked (401/403/429).';
      if (reason === 'quota_unreachable') return 'Provider quota endpoint unreachable.';
      return `Provider error: ${reason.replaceAll('_', ' ')}`;
    }
    if (state === 'unavailable') {
      return 'Provider quota is unavailable from its reporting source.';
    }
    return null;
  }

  function nativeText(win) {
    if (!win) return null;
    if (win.credits_remaining != null) return `${win.credits_remaining} credits`;
    if (win.spend_used != null) {
      return `spent ${win.spend_used}${win.unit && win.unit !== 'percent' ? ` ${win.unit}` : ''}`;
    }
    return null;
  }

  function mergeWindow(map, rawName, incoming) {
    if (!incoming) return;
    const key = WINDOW_LABELS[rawName] || rawName;
    const current = map.get(key);
    if (incoming.error === 'unavailable') {
      if (!current) map.set(key, { ...incoming });
      return;
    }
    if (incoming.present !== true) return;
    const incRem = percent(incoming.percent_remaining);
    const curRem = current && current.present === true ? percent(current.percent_remaining) : null;
    if (!current || current.present !== true) {
      map.set(key, { ...incoming });
      return;
    }
    const takeIncoming = incRem !== null && (curRem === null || incRem < curRem);
    map.set(key, {
      ...current,
      ...(takeIncoming ? incoming : {}),
      present: true,
      percent_remaining: takeIncoming ? incRem : curRem,
      reset_at: (takeIncoming ? incoming.reset_at : current.reset_at) || incoming.reset_at || current.reset_at || null,
      credits_remaining: current.credits_remaining ?? incoming.credits_remaining ?? null,
      spend_used: current.spend_used ?? incoming.spend_used ?? null,
      spend_limit: current.spend_limit ?? incoming.spend_limit ?? null,
      unit: (current.unit && current.unit !== 'percent' ? current.unit : incoming.unit) || null,
    });
  }

  function isLegacyRouteAccount(data, provider, account) {
    const canonProv = canonicalName(data, 'providers', provider);
    const canonAcct = canonicalName(data, 'accounts', account);
    return canonicalName(data, 'providers', canonAcct) === canonProv && canonAcct !== canonProv;
  }

  function snapshotRouteForPool(data, host, pool, allPools) {
    if (host.snapshot?.status !== 'ok' || host.snapshot.stale) return null;
    if (!(pool.observed_by_hosts || []).includes(host.host_id)) return null;
    const routes = host.snapshot.routes || {};
    const targetProvider = canonicalName(data, 'providers', pool.provider);
    const targetAccount = canonicalName(data, 'accounts', pool.account);
    if (routes[targetAccount] && canonicalName(data, 'providers', targetAccount) === targetProvider) {
      return routes[targetAccount];
    }
    if (isLegacyRouteAccount(data, targetProvider, targetAccount)) return null;
    const matchingPools = allPools.filter(candidate =>
      canonicalName(data, 'providers', candidate.provider) === targetProvider &&
      (candidate.observed_by_hosts || []).includes(host.host_id) &&
      !isLegacyRouteAccount(data, candidate.provider, candidate.account));
    if (matchingPools.length === 1 &&
        canonicalName(data, 'accounts', matchingPools[0].account) === targetAccount) {
      return routes[targetProvider] || null;
    }
    return null;
  }

  function windowsFor(data, pool, allPools) {
    const aggregated = new Map();
    for (const [name, win] of Object.entries(pool.windows || {})) {
      mergeWindow(aggregated, name, win);
    }
    if (pool.source !== 'server') {
      const targetProvider = canonicalName(data, 'providers', pool.provider);
      const targetAccount = canonicalName(data, 'accounts', pool.account);
      for (const host of data.hosts || []) {
        for (const sub of host.subscriptions || []) {
          if (sub.stale ||
              canonicalName(data, 'providers', sub.service) !== targetProvider ||
              canonicalName(data, 'accounts', sub.account) !== targetAccount ||
              sub.quota?.status !== 'ok' ||
              sub.quota.details?.stale === true) continue;
          for (const [name, win] of Object.entries(sub.quota.windows || {})) {
            mergeWindow(aggregated, name, win);
          }
        }
        const route = snapshotRouteForPool(data, host, pool, allPools);
        if (route?.status === 'ok' && route.details?.stale !== true) {
          for (const [name, win] of Object.entries(route.windows || {})) {
            mergeWindow(aggregated, name, win);
          }
        }
      }
    }
    const entries = [...aggregated.entries()].sort(([a], [b]) => {
      const ia = WINDOW_ORDER.indexOf(a);
      const ib = WINDOW_ORDER.indexOf(b);
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      return a.localeCompare(b);
    });
    const poolRem = percent(pool.remaining_percent);
    if (poolRem !== null && entries.length) {
      const minEntry = entries.reduce((best, cur) => {
        const rem = cur[1]?.present === true ? percent(cur[1].percent_remaining) : null;
        if (rem === null) return best;
        return !best || rem < best.rem ? { win: cur[1], rem } : best;
      }, null);
      if (minEntry && minEntry.rem > poolRem) {
        minEntry.win.percent_remaining = poolRem;
      }
    }
    return entries;
  }

  function collectBankedResets(data, pools, resetsSnapshot) {
    const banked = new Map();
    const bankedKnown = new Set();
    const resetChecksFailed = new Set();
    const addGrant = (provider, account, grant) => {
      const key = `${provider}\0${account}`;
      if (!banked.has(key)) banked.set(key, new Map());
      const expiresIso = typeof grant.expires_at === 'number'
        ? new Date(grant.expires_at).toISOString()
        : grant.expires_at;
      if (typeof expiresIso !== 'string' || !Number.isFinite(Date.parse(expiresIso))) return;
      const available = grant.available !== false;
      const id = `${expiresIso}\0${available}\0${grant.label || ''}`;
      banked.get(key).set(id, { expires_at: expiresIso, available, label: grant.label || '' });
    };
    const collectFromQuota = (provider, account, quota) => {
      if (!quota || quota.status !== 'ok' || quota.details?.stale === true) return;
      const key = `${provider}\0${account}`;
      if (quota.details?.banked_resets_error) {
        resetChecksFailed.add(key);
        return;
      }
      if (!Array.isArray(quota.details?.banked_resets)) return;
      bankedKnown.add(key);
      for (const grant of quota.details.banked_resets) {
        if (!grant || typeof grant.available !== 'boolean') continue;
        addGrant(provider, account, grant);
      }
    };
    for (const pool of pools) {
      if (!['fresh', 'exhausted'].includes(pool.state)) continue;
      const prov = canonicalName(data, 'providers', pool.provider);
      const acct = canonicalName(data, 'accounts', pool.account);
      const key = `${prov}\0${acct}`;
      if (pool.banked_resets_error || pool.details?.banked_resets_error) {
        resetChecksFailed.add(key);
        continue;
      }
      const list = Array.isArray(pool.banked_resets)
        ? pool.banked_resets
        : Array.isArray(pool.details?.banked_resets) ? pool.details.banked_resets : null;
      if (!list) continue;
      bankedKnown.add(key);
      for (const grant of list) {
        if (grant && typeof grant.available === 'boolean') addGrant(prov, acct, grant);
      }
    }
    for (const host of data.hosts || []) {
      for (const pool of pools) {
        const route = snapshotRouteForPool(data, host, pool, pools);
        if (route?.status === 'ok') {
          collectFromQuota(
            canonicalName(data, 'providers', pool.provider),
            canonicalName(data, 'accounts', pool.account),
            route,
          );
        }
      }
      for (const sub of host.subscriptions || []) {
        if (!sub?.stale) {
          collectFromQuota(
            canonicalName(data, 'providers', sub.service),
            canonicalName(data, 'accounts', sub.account),
            sub.quota,
          );
        }
      }
    }
    if (resetsSnapshot && Array.isArray(resetsSnapshot.grants)) {
      const poolByAccount = new Map(pools.map(pool => [
        canonicalName(data, 'accounts', pool.account),
        canonicalName(data, 'providers', pool.provider),
      ]));
      for (const obs of resetsSnapshot.observations || []) {
        const acct = canonicalName(data, 'accounts', obs.account);
        const prov = poolByAccount.get(acct);
        if (prov) bankedKnown.add(`${prov}\0${acct}`);
      }
      for (const grant of resetsSnapshot.grants) {
        const acct = canonicalName(data, 'accounts', grant.account);
        const prov = poolByAccount.get(acct);
        if (!prov) continue;
        bankedKnown.add(`${prov}\0${acct}`);
        addGrant(prov, acct, { ...grant, available: true });
      }
    }
    return { banked, bankedKnown, resetChecksFailed };
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
      `As of ${formatTime(data.as_of)}.`;
  }

  function render(data, fleet, resetsSnapshot, enginesData) {
    const list = $('quota-list');
    const summary = $('quota-summary');
    list.replaceChildren();
    summary.replaceChildren();

    const pools = Array.isArray(data.pools) ? data.pools : [];
    const accounts = fleet?.accounts || [];
    const byLabel = new Map(accounts.map(account => [
      canonicalName(data, 'accounts', account.label), account,
    ]));
    const credentialsByLabel = new Map((enginesData?.credentials || []).map(cred => [
      canonicalName(data, 'accounts', cred.label), cred,
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

    const { banked, bankedKnown, resetChecksFailed } = collectBankedResets(data, pools, resetsSnapshot);
    const sortedPools = [...pools].sort((a, b) =>
      canonicalName(data, 'providers', a.provider).localeCompare(canonicalName(data, 'providers', b.provider)) ||
      canonicalName(data, 'accounts', a.account).localeCompare(canonicalName(data, 'accounts', b.account)));

    for (const pool of sortedPools) {
      const providerKey = canonicalName(data, 'providers', pool.provider);
      const accountKey = canonicalName(data, 'accounts', pool.account);
      const account = byLabel.get(accountKey);
      const cred = credentialsByLabel.get(accountKey);
      const state = pool.state || 'unknown';
      const providerError = formatProviderError(pool, state, cred);
      const card = document.createElement('article');
      card.className = `quota-provider quota-provider--${state}`;

      const meta = document.createElement('div');
      meta.className = 'quota-provider__meta';
      const heading = document.createElement('div');
      heading.className = 'quota-provider__heading';
      heading.append(element('h3', displayProvider(providerKey)));
      if (accountKey.toLowerCase() !== providerKey.toLowerCase()) {
        heading.append(element('span', accountKey, 'quota-provider__account'));
      }
      if (providerError) {
        heading.append(element('span', 'error', 'quota-badge quota-badge--error'));
      }
      heading.append(element('span', state, `${stateClass(state)} quota-state`));
      if (pool.routing === 'disabled') {
        heading.append(element('span', 'disabled', 'quota-state quota-state--unknown'));
      }
      meta.append(heading);

      if (account) {
        const concurrency = `${account.active ?? '?'} / ${account.max_concurrency ?? '?'} active` +
          `${account.placeable_slots == null ? '' : ` · ${account.placeable_slots} placeable`}` +
          `${account.reserved ? ` · ${account.reserved} reserved` : ''}`;
        meta.append(element('p', concurrency, 'quota-provider__concurrency'));
      }
      card.append(meta);

      const body = document.createElement('div');
      body.className = 'quota-provider__body';
      const allWindows = windowsFor(data, pool, pools);
      const hasPresentWindow = allWindows.some(([, win]) => win?.present === true);
      const windows = ['fresh', 'exhausted'].includes(state) || hasPresentWindow
        ? allWindows
        : [];
      if (!windows.length) {
        const message = element('p',
          providerError || (state === 'stale'
            ? 'The last quota observation is stale.'
            : state === 'exhausted'
              ? 'Quota is exhausted; no active window was reported.'
              : pool.source === 'unmetered-engine'
                ? 'Unmetered engine · no provider window limit reported.'
                : 'Quota windows are unknown; no remaining amount was reported.'),
          `quota-provider__message${providerError ? ' quota-provider__message--error' : ''}`);
        body.append(message);
      } else {
        const rows = document.createElement('div');
        rows.className = 'quota-provider__windows';
        for (const [name, win] of windows) {
          const remaining = ['fresh', 'exhausted'].includes(state) && win?.present === true
            ? percent(win.percent_remaining)
            : null;
          const level = remaining === null
            ? 'unknown'
            : remaining <= 20
              ? 'low'
              : remaining <= 70
                ? 'mid'
                : 'high';
          const row = document.createElement('div');
          row.className = 'quota-provider__row';
          if (win?.error === 'unavailable') row.classList.add('quota-provider__row--error');
          const label = element('span', name, 'quota-provider__window');
          const meter = document.createElement('div');
          meter.className = `quota-meter quota-meter--${level}`;
          const amount = element('span', '', `quota-provider__percent quota-provider__percent--${level}`);
          if (remaining === null) {
            meter.textContent = win?.error === 'unavailable' || state === 'unavailable'
              ? 'unavailable'
              : state === 'stale' ? 'stale' : 'unknown';
            meter.setAttribute('aria-label', `${name}: ${meter.textContent}`);
          } else {
            const bar = document.createElement('span');
            bar.className = 'quota-meter__fill';
            bar.style.width = `${remaining}%`;
            bar.setAttribute('role', 'progressbar');
            bar.setAttribute('aria-valuemin', '0');
            bar.setAttribute('aria-valuemax', '100');
            bar.setAttribute('aria-valuenow', String(remaining));
            bar.setAttribute('aria-label', `${name}: ${remaining}% remaining`);
            meter.append(bar);
            amount.textContent = `${remaining}%`;
          }
          const reset = element('span',
            ['fresh', 'exhausted'].includes(state) && win?.present === true
              ? resetText(win.reset_at, remaining)
              : state === 'stale' ? 'reset unknown · stale data' : 'reset unknown',
            'quota-provider__reset');
          row.append(label, meter, amount, reset);
          rows.append(row);
        }
        body.append(rows);
      }

      const poolBankedKey = `${providerKey}\0${accountKey}`;
      const grants = ['fresh', 'exhausted'].includes(state)
        ? [...(banked.get(poolBankedKey)?.values() || [])]
        : [];
      if (grants.length || (['fresh', 'exhausted'].includes(state) && bankedKnown.has(poolBankedKey))) {
        const available = grants.filter(g => g.available && Number.isFinite(Date.parse(g.expires_at)) && Date.parse(g.expires_at) > Date.now());
        const expiries = available.map(g => Date.parse(g.expires_at)).filter(Number.isFinite).sort((a, b) => a - b);
        body.append(element('p',
          `${available.length} reset${available.length === 1 ? '' : 's'} available` +
          `${expiries.length ? ` · expires ${resetText(new Date(expiries[0]).toISOString())}` : ''}`,
          'quota-provider__resets'));
      } else if (['fresh', 'exhausted'].includes(state) && resetChecksFailed.has(poolBankedKey)) {
        body.append(element('p', 'Reset availability could not be verified.', 'quota-provider__resets quota-provider__resets--unknown'));
      }

      const details = document.createElement('details');
      details.className = 'quota-provider__details';
      details.append(element('summary', 'Source, native usage & burn details'));

      const source = pool.source === 'server'
        ? 'Hub provider probe'
        : pool.source === 'unmetered-engine'
          ? 'Unmetered engine'
          : `Machine report${pool.observed_by_hosts?.length ? ` · ${pool.observed_by_hosts.join(', ')}` : ''}`;
      const forecast = account?.burn_forecast;
      const burn = forecast
        ? `~${Math.round(forecast.projected_unused_percent)}% unused at ${forecast.window} reset in ${Math.round(forecast.hours_to_reset)}h` +
          (forecast.burn_rate_percent_per_hour == null ? ' (burn rate unknown)' : forecast.waste_alert ? ' · likely to waste' : '')
        : 'No burn forecast';
      details.append(element('p',
        `${state} · observed ${formatTime(pool.observed_at)} · Source: ${source} · ${burn}`,
        'quota-provider__freshness'));

      const detailLines = [];
      if (pool.native_balance) {
        detailLines.push(`Hub balance: ${pool.native_balance.remaining} ${pool.native_balance.unit === 'usd_cents' ? 'USD cents' : pool.native_balance.unit}`);
      }
      for (const [name, win] of windows) {
        const native = nativeText(win);
        if (native) detailLines.push(`${name}: ${native}`);
      }
      for (const host of data.hosts || []) {
        const route = snapshotRouteForPool(data, host, pool, pools);
        if (route) {
          for (const [name, win] of Object.entries(route.windows || {})) {
            const native = nativeText(win);
            if (native) detailLines.push(`${host.host_id} ${WINDOW_LABELS[name] || name}: ${native}`);
          }
        }
        for (const sub of host.subscriptions || []) {
          if (sub.stale ||
              canonicalName(data, 'providers', sub.service) !== providerKey ||
              canonicalName(data, 'accounts', sub.account) !== accountKey) continue;
          for (const [name, win] of Object.entries(sub.quota?.windows || {})) {
            const native = nativeText(win);
            if (native) detailLines.push(`${host.host_id} ${WINDOW_LABELS[name] || name}: ${native}`);
          }
        }
      }
      if (detailLines.length) {
        const ul = document.createElement('ul');
        for (const line of [...new Set(detailLines)]) {
          ul.append(element('li', line));
        }
        details.append(ul);
      }
      const resetLink = element('a', 'Review banked reset grants');
      resetLink.href = `/banked-resets.html?account=${encodeURIComponent(pool.account)}`;
      details.append(resetLink);
      body.append(details);
      card.append(body);
      list.append(card);
    }

    showCoverage(data);
  }

  async function refresh() {
    const status = $('quota-status');
    $('quota-refresh').disabled = true;
    try {
      const me = await get('/auth/me');
      for (const id of ['account-name', 'menu-name', 'menu-email']) $(id).textContent = me.email;
      document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0, 1).toUpperCase();
      const [quota, fleet, resetsSnapshot, enginesData] = await Promise.all([
        get('/api/quota?view=fleet'),
        get('/api/fleet?sections=accounts,machines'),
        get('/api/quota/resets').catch(() => null),
        get('/api/engines').catch(() => null),
      ]);
      render(quota, fleet, resetsSnapshot, enginesData);
    } catch (error) {
      status.textContent = `Quota data could not be refreshed. Any displayed data is from the last successful update. ${error.message}`;
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
