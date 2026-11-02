(() => {
  const $ = id => document.getElementById(id);
  let csrf = '';

  async function api(path, method = 'GET', data) {
    const response = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: data === undefined ? {} : {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
      },
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.json();
  }

  function text(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value || '';
    return node;
  }

  async function refreshChecks() {
    const root = $('checks-list');
    try {
      const data = await api('/api/checks');
      root.replaceChildren();
      if (!(data.checks || []).length) {
        root.append(text('p', 'No scheduled health checks are configured.'));
        return;
      }
      for (const check of data.checks) {
        const row = document.createElement('div');
        row.className = 'check-row';
        const lastRun = check.last_status || 'not run';
        const label = text('span',
          `${check.name} · ${check.enabled ? 'enabled' : 'disabled'} · ` +
          `every ${Math.round(check.interval_s / 60)} min · ${lastRun}`);
        const button = text('button', 'Run now');
        button.type = 'button';
        button.className = 'dk-button dk-button--secondary dk-button--sm';
        button.setAttribute('aria-label', `Run health check ${check.name} now`);
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            await api(`/api/checks/${encodeURIComponent(check.name)}/run`, 'POST', {});
            await refreshChecks();
            $('checks-result').textContent = `Check “${check.name}” was queued.`;
          } catch {
            $('checks-result').textContent = 'The check could not be run. Refresh to inspect its current state.';
          } finally {
            button.disabled = false;
          }
        });
        row.append(label, button);
        root.append(row);
      }
      for (const run of (data.runs || []).slice(0, 5)) {
        root.append(text('p', `Recent: ${run.name} · ${run.status} · ${run.at}`));
      }
    } catch {
      root.replaceChildren(text('p',
        'Health checks unavailable. Sign in with administrator access to view or run checks.'));
    }
  }

  $('registry-form').addEventListener('submit', async event => {
    event.preventDefault();
    const file = event.currentTarget.elements.registry.files[0];
    const button = event.submitter;
    if (!file || file.size > 32768) {
      $('registry-result').textContent = 'Choose a registry JSON under 32 KiB.';
      return;
    }
    button.disabled = true;
    try {
      await api('/api/fleet/accounts', 'PUT', JSON.parse(await file.text()));
      $('registry-result').textContent = 'Registry saved. Machines reconcile routes on their next heartbeat.';
    } catch {
      $('registry-result').textContent = 'Registry was not saved. Check the file and administrator access, then retry.';
    } finally {
      button.disabled = false;
    }
  });

  $('checks-form').addEventListener('submit', async event => {
    event.preventDefault();
    const file = event.currentTarget.elements.checks.files[0];
    const button = event.submitter;
    if (!file || file.size > 65536) {
      $('checks-result').textContent = 'Choose checks JSON under 64 KiB.';
      return;
    }
    button.disabled = true;
    try {
      await api('/api/checks', 'PUT', JSON.parse(await file.text()));
      $('checks-result').textContent = 'Health checks saved.';
      await refreshChecks();
    } catch {
      $('checks-result').textContent = 'Checks were not saved. Check the file and administrator access, then retry.';
    } finally {
      button.disabled = false;
    }
  });

  $('sign-out').addEventListener('click', async () => {
    try {
      await fetch('/auth/logout', {
        method: 'POST', credentials: 'same-origin', redirect: 'manual',
        headers: { 'X-CSRF-Token': csrf },
      });
      location.assign('/auth/login');
    } catch {
      $('settings-status').textContent = 'Sign-out could not be confirmed. Try again.';
    }
  });

  (async () => {
    try {
      const me = await api('/auth/me');
      csrf = me.csrf_token;
      for (const id of ['account-name', 'menu-name', 'menu-email']) $(id).textContent = me.email;
      document.querySelector('.dk-avatar__initials').textContent = me.email.slice(0, 1).toUpperCase();
      $('settings-status').textContent = 'Connected · changes require administrator access.';
      await refreshChecks();
      const panel = new URLSearchParams(location.search).get('panel');
      const target = panel === 'checks' ? 'checks-panel'
        : panel === 'accounts' ? 'registry-panel'
          : panel === 'policy' ? 'routing-policy-panel' : null;
      if (target) document.getElementById(target).scrollIntoView({ block: 'start' });
      setInterval(() => { if (!document.hidden) refreshChecks(); }, 60000);
    } catch {
      $('settings-status').textContent = 'Settings require an administrator sign-in.';
    }
  })();
})();
