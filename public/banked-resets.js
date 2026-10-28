let csrf;
const status = document.getElementById('status');
const accountSelect = document.getElementById('account');
const grantInput = document.getElementById('grants');
let snapshot;
const node = (tag, text) => { const e = document.createElement(tag); e.textContent = text; return e; };
const berlin = value => new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Berlin', dateStyle: 'medium', timeStyle: 'long' }).format(new Date(value));
async function call(path, method = 'GET', data) {
  const r = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(method === 'POST' ? { 'X-CSRF-Token': csrf } : {}) },
    ...(data ? { body: JSON.stringify(data) } : {}) });
  if (!r.ok) throw new Error('Request failed; check sign-in, account and expiry values.');
  return r.json();
}
function fillList() {
  grantInput.value = snapshot.grants.filter(g => g.account === accountSelect.value && g.expires_at > Date.now())
    .map(g => `${g.label} | ${new Date(g.expires_at).toISOString()}`).join('\n');
}
async function refresh() {
  snapshot = await call('/api/quota/resets');
  const selected = accountSelect.value;
  accountSelect.replaceChildren();
  const container = document.getElementById('accounts');
  container.replaceChildren();
  for (const a of snapshot.accounts) {
    const option = node('option', `${a.label} (${a.provider})`); option.value = a.label;
    accountSelect.append(option);
    const section = document.createElement('section');
    section.append(node('h2', `${a.label} (${a.provider})`));
    const observation = snapshot.observations.find(o => o.account === a.label);
    section.append(node('p', observation ? `Last recorded: ${berlin(observation.observed_at)} · ${observation.source}` : 'Expiry unknown — record each available grant or confirm none.'));
    const grants = snapshot.grants.filter(g => g.account === a.label);
    for (const g of grants) section.append(node('p', `${g.label} · ${berlin(g.expires_at)} · ${g.source}${g.expires_at <= Date.now() ? ' · expired' : ''}`));
    if (!grants.length && observation) section.append(node('p', 'No available grants recorded.'));
    container.append(section);
  }
  if ([...accountSelect.options].some(o => o.value === selected)) accountSelect.value = selected;
  fillList();
}
accountSelect.addEventListener('change', fillList);
document.getElementById('record-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const grants = grantInput.value.split('\n').filter(line => line.trim()).map(line => {
      const parts = line.split('|').map(v => v.trim());
      if (parts.length !== 2 || !parts[0] || !/(Z|[+-]\d{2}:\d{2})$/.test(parts[1]) || !Number.isFinite(Date.parse(parts[1]))) throw new Error('Use reset type | expiry with Z or an explicit UTC offset on every line.');
      return { label: parts[0], expires_at: parts[1] };
    });
    await call('/api/quota/resets', 'POST', { account: accountSelect.value, grants });
    await refresh();
    status.textContent = 'Saved. Hub will check deadlines on its next five-minute scheduled round.';
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
(async () => {
  try { const me = await call('/auth/me'); csrf = me.csrf_token; await refresh(); status.textContent = 'Connected. Alerts go through the configured Hub notification channels.'; }
  catch { status.textContent = 'Sign in as a Hub administrator to view and record reset grants.'; }
})();
