/* Theme choice only is stored. Sessions, credentials and CSRF stay out of DOM/storage. */
try {
  const theme = localStorage.getItem('dakit-theme');
  document.documentElement.dataset.theme = ['light', 'dark'].includes(theme) ? theme : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
} catch {}
addEventListener('DOMContentLoaded', () => {
  document.querySelector('h1')?.focus();
  // Use the official code primitive with Hub's actual issuance alphabet.
  import('/vendor/dakit/device-code.js').then(({bindCodeInput,normalizeUserCode,describeUserCodeError}) => {
    for (const input of document.querySelectorAll('.dk-code-input')) {
      const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
      bindCodeInput(input,{alphabet});
      input.form.addEventListener('submit',event => {
        const code = normalizeUserCode(input.value,{alphabet});
        let error = input.parentElement.querySelector('.dk-field-error');
        if (!error) { error = document.createElement('p'); error.className = 'dk-field-error'; error.id = input.id+'-error'; error.setAttribute('role','alert'); input.parentElement.append(error); }
        if (!code.ok) {
          event.preventDefault();event.stopImmediatePropagation();
          error.textContent = describeUserCodeError(code) || 'Enter the complete code from your terminal.';
          input.setAttribute('aria-invalid','true'); input.setAttribute('aria-describedby',error.id); input.focus();
        } else { input.removeAttribute('aria-invalid'); error.textContent = ''; input.value = code.display; }
      },true);
    }
  }).catch(() => { /* Normal GET code entry remains usable if module loading fails. */ });

  if (document.documentElement.dataset.hubPage === 'overview') {
    const status = document.getElementById('status');
    const signOut = document.getElementById('sign-out');
    let csrf = null;
    fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(me => {
        if (!me || !me.email) {
          if (status) status.textContent = 'Ready · sign in to access live hub data';
          return;
        }
        csrf = me.csrf_token || null;
        for (const id of ['account-name', 'menu-name', 'menu-email']) {
          const el = document.getElementById(id);
          if (el) el.textContent = me.email;
        }
        const initials = document.querySelector('.dk-avatar__initials');
        if (initials) initials.textContent = me.email.slice(0, 1).toUpperCase();
        if (status) status.textContent = `Authenticated as ${me.email}`;
      })
      .catch(() => {
        if (status) status.textContent = 'Workspace directory ready';
      });
    signOut?.addEventListener('click', async () => {
      signOut.disabled = true;
      try {
        if (!csrf) {
          const me = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
          if (me.ok) csrf = (await me.json()).csrf_token;
        }
        const res = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: csrf ? { 'X-CSRF-Token': csrf } : {} });
        if (res.type !== 'opaqueredirect' && !res.ok) throw Error('Sign-out failed');
        location.assign('/auth/login');
      } catch {
        if (status) status.textContent = 'Sign-out could not be confirmed. Try again.';
        signOut.disabled = false;
      }
    });
  }

  const review = document.getElementById('device-review');
  if (!review) return;
  const confirm = document.getElementById('device-confirm');
  confirm.addEventListener('submit', event => {
    event.preventDefault();
    const code = confirm.elements.code.value.trim().toUpperCase();
    if (code !== review.dataset.code) { location.assign('/device?code=' + encodeURIComponent(code)); return; }
    confirm.remove(); review.hidden = false;
    document.getElementById('auth-title').textContent = 'Approve this device?';
    document.querySelector('.dk-auth-card__lede').textContent = 'Only approve a login you started. Review the identity, permissions and machine below.';
    document.getElementById('auth-title').focus();
  });
  const form = document.getElementById('approveForm');
  const error = document.getElementById('device-error');
  const deny = document.getElementById('device-deny');
  const approve = document.getElementById('device-approve');
  let busy = false, finished = false, announced = false;
  const deadline = performance.now() + Number(review.dataset.remaining);
  const result = (title, lede, recovery) => {
    finished = true; clearInterval(timer); confirm?.remove();
    document.getElementById('auth-title').textContent = title;
    document.querySelector('.dk-auth-card__lede').textContent = lede;
    review.replaceChildren(); review.hidden = false;
    if (recovery) { const link = document.createElement('a'); link.className = 'dk-button dk-button--secondary'; link.href = '/device'; link.textContent = 'Enter another code'; review.append(link); }
    else { const p = document.createElement('p'); p.setAttribute('role', 'status'); p.textContent = 'You can close this page.'; review.append(p); }
    document.getElementById('auth-title').focus();
  };
  const countdown = () => {
    if (finished) return;
    const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
    document.getElementById('device-countdown').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    if (seconds <= 60 && !announced) { announced = true; document.getElementById('device-live').textContent = 'One minute left to decide.'; }
    if (seconds === 0 && !busy) result('Code expired', 'Run hub login again for a new code.', true);
  };
  const timer = setInterval(countdown, 1000); countdown();
  const decide = async action => {
    if (busy || finished) return;
    const scopes = [...form.querySelectorAll('[name=scope]:checked')].map(input => input.value);
    if (action === 'approve' && !scopes.length) { error.textContent = 'Select at least one requested permission, or deny the request.'; error.hidden = false; form.querySelector('[name=scope]')?.focus(); return; }
    busy = true; error.hidden = true; deny.disabled = approve.disabled = true;
    const controls = [...form.querySelectorAll('input,select')]; controls.forEach(control => control.disabled = true);
    const button = action === 'approve' ? approve : deny;
    button.textContent = action === 'approve' ? 'Approving…' : 'Denying…'; button.setAttribute('aria-busy', 'true');
    try {
      const me = await fetch('/auth/me', {credentials:'same-origin',cache:'no-store'});
      if (me.status === 401) { location.assign('/auth/login?return_to=' + encodeURIComponent('/device?code=' + review.dataset.code)); return; }
      if (!me.ok) throw Error('Sign-in could not be checked. Reload this request before deciding.');
      const session = await me.json();
      const data = {granted_scopes:scopes,expected_actor:review.dataset.actor || null};
      if (form.elements.machine_id?.value) data.machine_id = form.elements.machine_id.value;
      else if (review.dataset.kind === 'agent') data.machine_id = review.dataset.machine || null;
      const response = await fetch('/api/device/' + action + '/' + encodeURIComponent(review.dataset.code), {method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},...(action === 'approve' ? {body:JSON.stringify(data)} : {})});
      if (response.status === 401) { location.assign('/auth/login?return_to=' + encodeURIComponent('/device?code=' + review.dataset.code)); return; }
      if (!response.ok) {
        let detail; try { detail = (await response.json()).error; } catch {}
        if (detail === 'Already expired') { result('Code expired','Run hub login again for a new code.',true); return; }
        if (response.status === 404) { result('Code not found','This code is wrong, expired, or already used. Run hub login again for a new one.',true); return; }
        throw Error(response.status === 403 ? 'Your approval session could not be verified. Reload this request before deciding.' : response.status === 409 || detail === 'Already processed' || detail === 'invalid_pairing' ? 'This request changed or was already decided. Reload it to see its current state.' : response.status === 429 ? 'Too many requests. Wait a few minutes, then reload this request.' : response.status >= 500 ? 'The hub could not confirm your decision. Reload the request to check its state.' : 'The selected permissions or machine could not be approved. Review your choices or reload the request.');
      }
      result(action === 'approve' ? 'Device authorized' : 'Request denied',action === 'approve' ? 'Return to your terminal to finish signing in.' : 'Nothing was authorized. Run hub login again if you meant to sign in.');
    } catch (cause) {
      error.textContent = cause instanceof TypeError ? 'The hub could not confirm your decision. Check your connection, then reload this request to check its state.' : cause.message;
      error.hidden = false; document.getElementById('device-reload').hidden = false;
    } finally {
      busy = false; deny.disabled = approve.disabled = false; controls.forEach(control => control.disabled = false);
      approve.textContent = 'Approve'; deny.textContent = 'Deny'; button.removeAttribute('aria-busy'); countdown();
    }
  };
  form.addEventListener('submit', event => { event.preventDefault(); decide('approve'); });
  deny.addEventListener('click', () => decide('deny'));
});
