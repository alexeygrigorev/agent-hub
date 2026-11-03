(() => {
  const $ = id => document.getElementById(id);
  const age = value => {
    const time = Date.parse(value || '');
    return Number.isFinite(time) ? Date.now() - time : Infinity;
  };
  const fresh = value => age(value) >= 0 && age(value) <= 180000;
  const fmtGib = value => Number.isFinite(value) ? `${value.toFixed(1)} GiB` : 'unknown';

  function labeledValue(label, value) {
    const wrap = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value || 'unknown';
    wrap.append(dt, dd);
    return wrap;
  }

  async function get(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.json();
  }

  function render(fleet, health, live, disk) {
    const list = $('machine-list');
    const summary = $('machine-summary');
    list.replaceChildren();
    summary.replaceChildren();

    const allMachines = Array.isArray(fleet.machines) ? fleet.machines : [];
    const machines = allMachines.filter(machine => !machine.alias_of);
    const resources = new Map((fleet.host_resources || []).map(host => [host.machine_id, host]));
    const healthById = new Map((health.machines || []).map(host => [host.machine_id, host]));
    const liveness = new Map((live.subjects || [])
      .filter(subject => subject.kind === 'machine')
      .map(subject => [subject.id, subject]));
    const machineState = machine => machine.drained
      ? 'Drained'
      : fresh(machine.last_checkin_at)
        ? 'Online'
        : machine.last_checkin_at ? 'Stale' : 'No check-in';
    const online = machines.filter(machine => fresh(machine.last_checkin_at) && !machine.drained).length;
    const attention = machines.filter(machine =>
      !fresh(machine.last_checkin_at) || healthById.get(machine.id)?.placement?.reason ||
      liveness.get(machine.id)?.state === 'offline').length;

    for (const [count, label] of [
      [machines.length, 'Machines'],
      [online, 'Online'],
      [attention, 'Needs attention'],
    ]) {
      const card = document.createElement('div');
      const strong = document.createElement('strong');
      const span = document.createElement('span');
      strong.textContent = String(count);
      span.textContent = label;
      card.append(strong, span);
      summary.append(card);
    }

    if (!machines.length) {
      const empty = document.createElement('p');
      empty.className = 'machine-empty';
      empty.textContent = 'No machines are registered yet. Pair a device in Settings to add one.';
      list.append(empty);
      return;
    }

    for (const machine of machines) {
      const hostHealth = healthById.get(machine.id) || {};
      const resource = resources.get(machine.id) || {};
      const state = machineState(machine);
      const pressure = hostHealth.placement?.reason || resource.placement_reason || '';
      const memory = hostHealth.memory || {};
      const availableMemory = Number.isFinite(resource.mem_avail_mb)
        ? `${Math.round(resource.mem_avail_mb)} MiB available`
        : memory.available_bytes != null
          ? `${fmtGib(memory.available_bytes / 1073741824)} available`
          : 'memory unknown';
      const running = resource.running ?? hostHealth.running ?? null;
      const queued = resource.queued ?? hostHealth.queued ?? null;
      const concurrency = running === null
        ? 'concurrency unknown'
        : `${running} running${queued === null ? '' : ` · ${queued} queued`}`;

      const article = document.createElement('article');
      article.className = 'machine-card';
      const heading = document.createElement('div');
      heading.className = 'machine-card__heading';
      const title = document.createElement('h2');
      title.textContent = machine.name || machine.id;
      const badge = document.createElement('span');
      badge.className = `machine-status ${state === 'Online' && !pressure ? 'machine-status--healthy' : 'machine-status--problem'}`;
      badge.textContent = pressure ? `${state} · ${pressure.replaceAll('_', ' ')}` : state;
      heading.append(title, badge);
      article.append(heading);

      const blocker = pressure || (machine.drained
        ? 'Machine is drained'
        : fresh(machine.last_checkin_at) ? 'None reported' : 'Awaiting a fresh check-in');
      const meta = document.createElement('dl');
      meta.className = 'machine-card__meta';
      meta.append(
        labeledValue('Capacity', `${concurrency} · ${availableMemory}`),
        labeledValue('Blocker', blocker),
        labeledValue('Last check-in', machine.last_checkin_at || 'Never reported'),
      );
      article.append(meta);

      const diagnostics = document.createElement('details');
      const diagnosticSummary = document.createElement('summary');
      diagnosticSummary.textContent = 'Diagnostics';
      diagnostics.append(diagnosticSummary);
      const aliases = Object.entries(fleet.machine_aliases || {})
        .filter(([, canonical]) => canonical === machine.id)
        .map(([id]) => id);
      const engines = [...new Set((fleet.routes || [])
        .filter(route => route.machine_id === machine.id)
        .map(route => route.engine))];
      const processes = hostHealth.processes || {};
      const filesystems = (disk?.machines || [])
        .find(host => host.machine_id === machine.id)?.filesystems || [];
      const diskText = filesystems.length
        ? filesystems.map(fs => {
          const free = Number.isFinite(fs.free_bytes)
            ? `${(fs.free_bytes / 1073741824).toFixed(1)} GiB free`
            : 'free space unknown';
          return `${fs.mount_point}: ${fs.status || 'unknown'} · ${fs.percent ?? 'unknown'}% used · ${free}`;
        }).join('; ')
        : 'No disk report';
      const cpuLoad = hostHealth.load
        ? [hostHealth.load.load1, hostHealth.load.load5, hostHealth.load.load15]
          .map(value => Number.isFinite(value) ? value.toFixed(2) : 'unknown').join(' / ')
        : 'unknown';
      const lines = [
        `Machine ID: ${machine.id}`,
        `Aliases: ${aliases.join(', ') || 'none'}`,
        `Observed: ${hostHealth.observed_at || machine.last_checkin_at || 'unknown'}`,
        `Host freshness: ${hostHealth.freshness || (fresh(machine.last_checkin_at) ? 'fresh' : 'unknown')}`,
        `CPU load (1m / 5m / 15m): ${cpuLoad}`,
        `Memory: ${memory.percent ?? 'unknown'}% used · ${availableMemory}`,
        `Processes / threads / forks: ${processes.process_count ?? 'unknown'} / ${processes.thread_count ?? 'unknown'} / ${processes.forks_per_sec ?? 'unknown'} per second`,
        `Launcher headroom: ${Number.isFinite(resource.headroom_mb) ? `${Math.round(resource.headroom_mb)} MiB` : 'unknown'} · floor ${Number.isFinite(resource.floor_mb) ? `${Math.round(resource.floor_mb)} MiB` : 'unknown'}`,
        `Disk: ${diskText}`,
        `Reported liveness: ${liveness.get(machine.id)?.state || 'unknown'}`,
        `Routed engines: ${engines.join(', ') || 'unknown'}`,
      ];
      for (const text of lines) {
        const line = document.createElement('p');
        line.textContent = text;
        diagnostics.append(line);
      }
      article.append(diagnostics);

      const historyButton = document.createElement('button');
      historyButton.type = 'button';
      historyButton.className = 'dk-button dk-button--secondary dk-button--sm';
      historyButton.textContent = 'Host history';
      historyButton.addEventListener('click', () => {
        const panel = $('machine-history');
        panel.open = true;
        location.hash = 'machine-history';
        panel.scrollIntoView({ block: 'start' });
        window.HubHostCharts?.selectMachine(machine.id);
      });
      article.append(historyButton);
      list.append(article);
    }
  }

  async function refresh() {
    const status = $('machines-status');
    $('machines-refresh').disabled = true;
    status.textContent = 'Refreshing machine status…';
    try {
      const me = await get('/auth/me');
      for (const id of ['account-name', 'menu-name', 'menu-email']) $(id).textContent = me.email;
      document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0, 1).toUpperCase();
      const fleet = await get('/api/fleet');
      const telemetry = await Promise.all([
        '/api/monitoring/health', '/api/liveness', '/api/monitoring/disk',
      ].map(path => get(path).catch(() => null)));
      const [health, live, disk] = telemetry;
      render(fleet, health || { machines: [] }, live || { subjects: [] }, disk);
      await window.HubHostCharts?.refresh(get);
      if (location.hash === '#machine-history') $('machine-history').open = true;
      status.textContent = `Updated ${new Date().toLocaleTimeString()}; unavailable or older than three-minute host telemetry is shown as unknown or stale.`;
    } catch (error) {
      status.textContent = `Machine data could not be refreshed. ${error.message}. Unknown capacity is not treated as available.`;
    } finally {
      $('machines-refresh').disabled = false;
    }
  }

  $('machines-refresh').addEventListener('click', refresh);
  $('sign-out').addEventListener('click', async () => {
    try {
      const me = await get('/auth/me');
      await fetch('/auth/logout', {
        method: 'POST', credentials: 'same-origin', redirect: 'manual',
        headers: { 'X-CSRF-Token': me.csrf_token },
      });
      location.assign('/auth/login');
    } catch {
      $('machines-status').textContent = 'Sign-out could not be confirmed. Try again.';
    }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
