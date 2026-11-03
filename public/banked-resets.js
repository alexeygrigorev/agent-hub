let csrf;
const status = document.getElementById('status');
const accountSelect = document.getElementById('account');
const grantInput = document.getElementById('grants');
let snapshot;

const node = (tag, text) => {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
};
const millis = value => typeof value === 'number' ? value : Date.parse(value);
const berlin = value => new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Europe/Berlin', dateStyle: 'medium', timeStyle: 'long',
}).format(new Date(value));

async function call(path, method = 'GET', data) {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(method === 'POST' ? { 'X-CSRF-Token': csrf } : {}),
    },
    ...(data ? { body: JSON.stringify(data) } : {}),
  });
  if (!response.ok) throw new Error('Request failed; check sign-in, account and expiry values.');
  return response.json();
}

function previewReplacement() {
  if (!snapshot) return;
  const current = snapshot.grants.filter(grant =>
    grant.account === accountSelect.value && grant.expires_at > Date.now());
  const proposed = [];
  for (const line of grantInput.value.split('\n').filter(value => value.trim())) {
    const parts = line.split('|').map(value => value.trim());
    const expires = parts[1] ? Date.parse(parts[1]) : NaN;
    if (parts.length !== 2 || !parts[0] || !Number.isFinite(expires)) {
      document.getElementById('replace-preview').textContent =
        'Fix the line format before saving. Save replaces the entire grant list for this account.';
      return;
    }
    proposed.push({ label: parts[0], expires_at: expires });
  }

  const key = grant => `${grant.label.toLocaleLowerCase()}|${millis(grant.expires_at)}`;
  const next = new Set(proposed.map(key));
  const old = new Set(current.map(key));
  const removed = current.filter(grant => !next.has(key(grant)));
  const added = proposed.filter(grant => !old.has(key(grant)));
  const summarize = (label, grants) => grants.length
    ? `${label}: ${grants.map(grant => `${grant.label} (${berlin(grant.expires_at)})`).join('; ')}`
    : `${label}: none`;
  document.getElementById('replace-preview').textContent =
    `Saving replaces all ${current.length} current unexpired grants for ${accountSelect.value || 'this account'}. ` +
    `${summarize('Removed', removed)}. ${summarize('Added', added)}. ${proposed.length - added.length} retained.`;
}

function fillList() {
  grantInput.value = snapshot.grants
    .filter(grant => grant.account === accountSelect.value && grant.expires_at > Date.now())
    .map(grant => `${grant.label} | ${new Date(grant.expires_at).toISOString()}`)
    .join('\n');
  previewReplacement();
}

async function refresh() {
  snapshot = await call('/api/quota/resets');
  const selected = accountSelect.value || new URLSearchParams(location.search).get('account') || '';
  accountSelect.replaceChildren();
  const container = document.getElementById('accounts');
  container.replaceChildren();
  for (const account of snapshot.accounts) {
    const option = node('option', `${account.label} (${account.provider})`);
    option.value = account.label;
    accountSelect.append(option);
    const section = document.createElement('section');
    section.append(node('h2', `${account.label} (${account.provider})`));
    const observation = snapshot.observations.find(item => item.account === account.label);
    section.append(node('p', observation
      ? `Last recorded: ${berlin(observation.observed_at)} · ${observation.source}`
      : 'Expiry unknown — record each available grant or confirm none.'));
    const grants = snapshot.grants.filter(grant => grant.account === account.label);
    for (const grant of grants) {
      section.append(node('p', `${grant.label} · ${berlin(grant.expires_at)} · ${grant.source}` +
        `${grant.expires_at <= Date.now() ? ' · expired' : ''}`));
    }
    if (!grants.length && observation) section.append(node('p', 'No available grants recorded.'));
    container.append(section);
  }

  const upcoming = snapshot.grants
    .filter(grant => grant.expires_at > Date.now())
    .sort((a, b) => a.expires_at - b.expires_at)[0];
  const next = document.getElementById('next-expiry');
  next.replaceChildren();
  if (upcoming) {
    const remaining = upcoming.expires_at - Date.now();
    const action = remaining < 24 * 3600000
      ? 'Action needed within 24 hours'
      : remaining <= 48 * 3600000 ? 'Review within 48 hours' : 'No immediate action';
    next.append(
      node('h2', `Next expiry · ${action}`),
      node('p', `${upcoming.label} · ${upcoming.account} · ${berlin(upcoming.expires_at)} · ` +
        `recorded via ${upcoming.source || 'unknown source'}`),
      node('p', 'Owner: Alexey · manual approval required before any reset is applied.'),
    );
  } else {
    next.append(
      node('h2', 'Next recorded expiry'),
      node('p', 'No future reset expiry is recorded. Fresh quota status is available on the Quota page.'),
    );
  }

  if ([...accountSelect.options].some(option => option.value === selected)) {
    accountSelect.value = selected;
  }
  fillList();
}

accountSelect.addEventListener('change', fillList);
grantInput.addEventListener('input', previewReplacement);
document.getElementById('record-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const grants = grantInput.value.split('\n').filter(line => line.trim()).map(line => {
      const parts = line.split('|').map(value => value.trim());
      if (parts.length !== 2 || !parts[0] || !/(Z|[+-]\d{2}:\d{2})$/.test(parts[1]) ||
          !Number.isFinite(Date.parse(parts[1]))) {
        throw new Error('Use reset type | expiry with Z or an explicit UTC offset on every line.');
      }
      return { label: parts[0], expires_at: parts[1] };
    });
    await call('/api/quota/resets', 'POST', { account: accountSelect.value, grants });
    await refresh();
    status.textContent = 'Saved. Hub will check deadlines on its next five-minute scheduled round.';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

(async () => {
  try {
    const me = await call('/auth/me');
    csrf = me.csrf_token;
    await refresh();
    status.textContent = 'Connected. Alerts go through the configured Hub notification channels.';
  } catch {
    status.textContent = 'Sign in as a Hub administrator to view and record reset grants.';
  }
})();
