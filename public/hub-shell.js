import { mountHubSearch } from '/hub-search.js';

const destinations = [
  { label: 'Sessions', href: '/sessions.html', group: 'primary' },
  { label: 'Tasks', href: '/intake.html', group: 'primary' },
  { label: 'Quota', href: '/quota.html', group: 'primary' },
  { label: 'Machines', href: '/machines.html', group: 'primary' },
  { label: 'Messages', href: '/inbox.html', group: 'primary' },
  { label: 'Metrics', href: '/metrics.html', group: 'more' },
  { label: 'Automation', href: '/schedules.html', group: 'more' },
  { label: 'Settings', href: '/settings.html', group: 'more' },
];

const pageNames = new Map([
  ['/index.html', 'Sessions'], ['/sessions.html', 'Sessions'], ['/intake.html', 'Tasks'],
  ['/fleet.html', location.hash === '#machines' ? 'Machines' : 'Quota'], ['/quota.html', 'Quota'], ['/machines.html', 'Machines'],
  ['/settings.html', 'Settings'], ['/inbox.html', 'Messages'],
  ['/conversations.html', 'Messages'], ['/metrics.html', 'Metrics'], ['/schedules.html', 'Automation'],
  ['/prompts.html', 'Automation'], ['/webhooks.html', 'Automation'], ['/engines.html', 'Settings'],
  ['/banked-resets.html', 'Settings'], ['/asks.html', 'Tasks'],
]);

function currentPath() {
  if (location.pathname === '/') return '/index.html';
  if (location.pathname.startsWith('/intake/')) return '/intake.html';
  if (['/sessions', '/intake', '/fleet', '/inbox', '/conversations', '/metrics', '/schedules',
    '/quota', '/machines', '/settings', '/prompts', '/webhooks', '/engines', '/banked-resets', '/asks'].includes(location.pathname)) {
    return `${location.pathname}.html`;
  }
  return location.pathname;
}

function link(item, current) {
  const a = document.createElement('a');
  a.className = 'hub-shell__nav-link';
  a.href = item.href;
  a.textContent = item.label;
  if (current === item.label) a.setAttribute('aria-current', 'page');
  return a;
}

function accountMenu() {
  const wrap = document.createElement('details');
  wrap.className = 'hub-shell__account';
  const summary = document.createElement('summary');
  summary.textContent = 'Account';
  const panel = document.createElement('div');
  panel.className = 'hub-shell__account-panel';
  const name = document.createElement('span');
  name.className = 'hub-shell__account-name';
  name.textContent = 'Signed in';
  const signOut = document.createElement('button');
  signOut.type = 'button';
  signOut.textContent = 'Sign out';
  signOut.addEventListener('click', async () => {
    signOut.disabled = true;
    try {
      const me = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
      if (!me.ok) throw new Error('session_unavailable');
      const data = await me.json();
      const res = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin', redirect: 'manual', headers: data.csrf_token ? { 'X-CSRF-Token': data.csrf_token } : {} });
      if (res.type !== 'opaqueredirect' && !res.ok) throw new Error('logout_failed');
      location.assign('/auth/login');
    } catch {
      signOut.disabled = false;
      signOut.textContent = 'Sign out unavailable';
    }
  });
  panel.append(name, signOut); wrap.append(summary, panel);
  fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(me => {
    if (me?.email) { name.textContent = me.email; summary.textContent = me.email.split('@')[0]; }
  }).catch(() => {});
  return wrap;
}

export function mountHubShell() {
  const path = currentPath();
  const fleetMachine = path === '/fleet.html' && (location.hash === '#machine-view' || new URLSearchParams(location.search).has('machine'));
  const current = path === '/metrics.html' && location.hash === '#routing-settings' ? 'Settings' : fleetMachine ? 'Machines' : pageNames.get(path);
  if (!current || document.querySelector('[data-hub-shell]')) return;

  const css = document.createElement('link');
  css.rel = 'stylesheet'; css.href = '/hub-shell.css';
  document.head.append(css);

  document.body.classList.add('hub-shell-active');
  const root = document.createElement('div');
  root.className = 'hub-shell'; root.dataset.hubShell = 'true';
  root.dataset.page = path;
  const skip = document.createElement('a'); skip.className = 'hub-shell__skip'; skip.href = '#main-content'; skip.textContent = 'Skip to content';
  const header = document.createElement('header');
  header.className = 'hub-shell__header';
  const brand = document.createElement('a'); brand.className = 'hub-shell__brand'; brand.href = '/sessions.html'; brand.textContent = 'Agent Hub';
  const searchButton = document.createElement('button'); searchButton.type = 'button'; searchButton.className = 'hub-shell__search';
  searchButton.setAttribute('aria-label', 'Search sessions, tasks, and machines');
  searchButton.innerHTML = '<span>Search sessions, tasks, machines…</span><kbd>⌘ K</kbd>';
  const launch = document.createElement('button'); launch.type = 'button'; launch.className = 'hub-shell__new'; launch.textContent = 'New session';
  launch.addEventListener('click', () => {
    // The sessions page may claim this event to mount its focused dialog. Until
    // that page integration is present, the existing launch form is the safe fallback.
    const event = new CustomEvent('hub:new-session', { bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    if (!event.defaultPrevented) location.assign('/fleet.html#launch');
  });
  const attention = document.createElement('a'); attention.className = 'hub-shell__attention'; attention.href = '/sessions.html?filter=attention'; attention.textContent = 'Needs attention';
  header.append(brand, searchButton, launch, attention, accountMenu());

  const sidebar = document.createElement('nav'); sidebar.className = 'hub-shell__sidebar'; sidebar.setAttribute('aria-label', 'Primary navigation');
  const primary = document.createElement('div'); primary.className = 'hub-shell__nav-group';
  const more = document.createElement('div'); more.className = 'hub-shell__nav-group hub-shell__nav-group--more';
  for (const item of destinations) (item.group === 'primary' ? primary : more).append(link(item, current));
  sidebar.append(primary, more);

  const mobile = document.createElement('nav'); mobile.className = 'hub-shell__bottom'; mobile.setAttribute('aria-label', 'Main navigation');
  for (const item of destinations.filter(item => ['Sessions', 'Tasks', 'Quota'].includes(item.label))) mobile.append(link(item, current));
  const moreButton = document.createElement('button'); moreButton.type = 'button'; moreButton.className = 'hub-shell__more'; moreButton.textContent = 'More';
  const moreMenu = document.createElement('div'); moreMenu.className = 'hub-shell__mobile-more'; moreMenu.hidden = true;
  moreButton.setAttribute('aria-expanded', 'false'); moreButton.setAttribute('aria-controls', 'hub-shell-more-menu');
  moreMenu.id = 'hub-shell-more-menu';
  for (const item of destinations.filter(item => ['Machines', 'Messages', 'Metrics', 'Automation', 'Settings'].includes(item.label))) moreMenu.append(link(item, current));
  moreButton.addEventListener('click', () => {
    moreMenu.hidden = !moreMenu.hidden;
    moreButton.setAttribute('aria-expanded', String(!moreMenu.hidden));
    if (!moreMenu.hidden) moreMenu.querySelector('a')?.focus();
  });
  mobile.append(moreButton, moreMenu);
  root.append(skip, header, sidebar, mobile);
  document.body.prepend(root);

  mountHubSearch(searchButton);
}
