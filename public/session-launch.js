/* Standalone launch dialog. Shared shell buttons dispatch hub:new-session. */
(() => {
  let dialog, csrf, stylesAdded = false;

  function ensureStyles() {
    if (stylesAdded) return;
    stylesAdded = true;
    if (document.querySelector('link[href="/sessions.css"]')) return;
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/sessions.css'; document.head.append(link);
  }

  function field(label, name, type = 'text', options = {}) {
    const wrap = document.createElement('label');
    wrap.className = 'dk-field';
    const caption = document.createElement('span'); caption.className = 'dk-field__label'; caption.textContent = label;
    let input;
    if (options.choices) {
      input = document.createElement('select'); input.className = 'dk-select';
      for (const [value, text] of options.choices) { const option = document.createElement('option'); option.value = value; option.textContent = text; input.append(option); }
    } else if (type === 'textarea') {
      input = document.createElement('textarea'); input.rows = 4; input.className = 'dk-textarea';
    } else {
      input = document.createElement('input'); input.type = type; input.className = 'dk-input';
    }
    input.name = name;
    if (options.required) input.required = true;
    if (options.maxLength) input.maxLength = options.maxLength;
    if (options.placeholder) input.placeholder = options.placeholder;
    wrap.append(caption, input);
    return wrap;
  }

  function ensureDialog() {
    if (dialog) return dialog;
    ensureStyles();
    dialog = document.createElement('dialog');
    dialog.className = 'session-launch-dialog';
    dialog.setAttribute('aria-labelledby', 'session-launch-title');
    const form = document.createElement('form'); form.className = 'dk-stack session-launch-form';
    const header = document.createElement('div'); header.className = 'session-launch-dialog__head';
    const title = document.createElement('h2'); title.id = 'session-launch-title'; title.textContent = 'New session';
    const close = document.createElement('button'); close.type = 'button'; close.className = 'dk-button dk-button--secondary dk-button--sm'; close.textContent = 'Close'; close.addEventListener('click', () => dialog.close());
    header.append(title, close);
    const intro = document.createElement('p'); intro.className = 'dk-muted'; intro.textContent = 'Choose what to work on. The session starts only after you submit this form.';
    const mode = field('Session type', 'mode', 'select', { choices: [['headless', 'Headless agent'], ['interactive', 'Interactive terminal']] });
    const machine = field('Machine', 'machine', 'select', { choices: [['', 'Choose automatically']] });
    const tier = field('Routing policy', 'tier', 'select', { choices: [['', 'Automatic (recommended)'], ['quick', 'Quick'], ['daily', 'Daily'], ['deep', 'Deep']] });
    const workspace = field('Workspace label', 'workspace', 'text', { required: true, placeholder: 'A configured workspace' });
    const summary = field('Task summary', 'summary', 'text', { required: true, maxLength: 240, placeholder: 'A short description of the work' });
    const prompt = field('Goal', 'prompt', 'textarea', { required: true, maxLength: 16000, placeholder: 'Describe the work to perform' });
    const advanced = document.createElement('details'); advanced.className = 'session-launch-advanced';
    const advancedTitle = document.createElement('summary'); advancedTitle.textContent = 'Advanced overrides';
    const advancedBody = document.createElement('div'); advancedBody.className = 'dk-stack';
    advancedBody.append(field('Engine override', 'engine', 'text', { placeholder: 'Leave empty for routing policy' }), field('Override reason', 'engine_override_reason', 'text', { placeholder: 'Required when overriding the engine' }));
    advanced.append(advancedTitle, advancedBody);
    const result = document.createElement('p'); result.className = 'dk-muted'; result.setAttribute('role', 'status'); result.setAttribute('aria-live', 'polite');
    const actions = document.createElement('div'); actions.className = 'session-launch-dialog__actions';
    const submit = document.createElement('button'); submit.type = 'submit'; submit.className = 'dk-button dk-button--primary'; submit.textContent = 'Start session';
    actions.append(submit);
    form.append(header, intro, mode, machine, tier, workspace, summary, prompt, advanced, result, actions);
    form.addEventListener('change', event => {
      if (event.target.name === 'mode') {
        const interactive = event.target.value === 'interactive';
        const goal = prompt.querySelector('textarea'), machineSelect = machine.querySelector('select');
        goal.required = !interactive; goal.disabled = interactive; if (interactive) goal.value = '';
        prompt.querySelector('.dk-field__label').textContent = interactive ? 'Goal (headless only)' : 'Goal';
        machineSelect.required = interactive;
        machine.querySelector('.dk-field__label').textContent = interactive ? 'Machine (required for interactive terminal)' : 'Machine';
      }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const values = Object.fromEntries([...new FormData(form)].filter(([, value]) => value !== ''));
      if (values.engine && !String(values.engine_override_reason || '').trim()) { result.textContent = 'Add a reason for this engine override.'; return; }
      form.dataset.key ||= crypto.randomUUID();
      submit.disabled = true; result.textContent = 'Submitting launch request…';
      try {
        if (!csrf) { const me = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' }); if (!me.ok) throw new Error('Sign-in required'); csrf = (await me.json()).csrf_token; }
        const response = await fetch('/api/fleet/launches', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ ...values, idempotency_key: form.dataset.key }) });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `Launch request failed (${response.status})`);
        result.textContent = `Launch queued on ${payload.machine || 'the selected machine'} (${payload.engine || 'automatic routing'}).`;
        form.reset(); delete form.dataset.key;
        setTimeout(() => { if (dialog.open) dialog.close('submitted'); }, 900);
      } catch (error) { result.textContent = `${error.message || 'Launch request failed.'} Retry safely with the same request key.`; }
      finally { submit.disabled = false; }
    });
    dialog.append(form);
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
    document.body.append(dialog);
    fetch('/api/fleet?sections=machines', { credentials: 'same-origin', cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(data => {
      const select = machine.querySelector('select');
      for (const host of data?.machines || []) { const option = document.createElement('option'); option.value = host.id; option.textContent = host.name ? `${host.name} · ${host.id}` : host.id; select.append(option); }
    }).catch(() => {});
    return dialog;
  }

  document.addEventListener('hub:new-session', () => {
    const target = ensureDialog(); if (!target.open) target.showModal();
    target.querySelector('[name="workspace"]')?.focus();
  });
  document.addEventListener('click', event => {
    const trigger = event.target.closest?.('[data-hub-new-session]');
    if (!trigger) return;
    event.preventDefault(); document.dispatchEvent(new CustomEvent('hub:new-session'));
  });
})();
