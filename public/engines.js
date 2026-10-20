/* Only provider consent links and public account metadata enter DOM. No tokens/cache storage. */
let csrf;
const status = document.getElementById('status');
const el = (tag, text) => { const node = document.createElement(tag); if (text) node.textContent = text; return node; };
async function call(path, method = 'GET', data) {
  const response = await fetch(path, {method, credentials:'same-origin', cache:'no-store',
    headers:{'Content-Type':'application/json', ...(method !== 'GET' ? {'X-CSRF-Token':csrf} : {})},
    ...(data ? {body:JSON.stringify(data)} : {})});
  if (!response.ok) throw new Error('Request failed');
  return response.json();
}
async function refresh() {
  const data = await call('/api/engines');
  const accounts = document.getElementById('accounts'); accounts.replaceChildren();
  for (const a of data.engines) {
    const section = el('section'); section.append(el('h2',a.label),el('p',`${a.provider} ${a.version}`));
    for (const install of data.installations.filter(i => i.label === a.label)) {
      const row = el('p',`${install.machine_id}: installed`);
      if (a.provider === 'gemini') {
        const button = el('button','Sign in with Google'); button.type = 'button'; button.className='dk-button dk-button--secondary';
        button.onclick = async () => { button.disabled=true; try {await call('/api/engines/logins','POST',{label:a.label,machine:install.machine_id}); await refresh();} catch {status.textContent='Unable to request sign-in.';} finally {button.disabled=false;} };
        row.append(button);
      }
      section.append(row);
    }
    accounts.append(section);
  }
  const logins = document.getElementById('logins');
  // Keep an in-progress authorization-code input intact during refresh.
  if (logins.querySelector('input:focus')) return;
  logins.replaceChildren();
  for (const item of data.logins || []) {
    const section = el('section'); section.append(el('h3',`${item.label} · ${item.machine_id}`),el('p',item.status));
    if (item.status === 'waiting' && item.url) {
      const url = new URL(item.url);
      if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com' || url.username) continue;
      const link = el('a','Open Google sign-in'); link.href=url.href; link.target='_blank'; link.rel='noopener noreferrer'; section.append(link);
      const form=el('form'), label=el('label','Authorization code '), input=el('input'), button=el('button','Complete sign-in');
      button.className='dk-button dk-button--primary'; input.className='dk-input'; input.type='password'; input.autocomplete='off'; input.required=true; input.maxLength=2048; label.append(input); form.append(label,button);
      form.onsubmit=async event => {event.preventDefault(); const code=input.value.trim(); input.value=''; button.disabled=true; try {await call('/api/engines/logins/'+item.id,'POST',{code}); await refresh();} catch {status.textContent='Sign-in code could not be submitted.';button.disabled=false;} };
      section.append(form);
    }
    logins.append(section);
  }
  status.textContent='Connected. Machines reconcile on their next heartbeat.';
}
(async () => {try {const me=await call('/auth/me'); csrf=me.csrf_token; await refresh(); setInterval(() => refresh().catch(() => {status.textContent='Refresh failed.';}),5000);} catch {status.textContent='Sign in as a Hub administrator to manage accounts.';}})();
