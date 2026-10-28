// F7 phone-width browser test: every Hub page at 390x844, light + dark.
// Real loopback Worker (production bundle incl. intake routes) + Hub + SQLite;
// positive API responses never mocked. Static layout pass first (no auth),
// then authenticated functional pass for /intake (filter, read-only open,
// note, status, approve) and ack-truthfulness on sessions/conversations.
//
// Run: node test/phone-ui.browser.mjs [--out DIR]
// Also invoked from scripts/test-full-local.sh and CI (deploy.yml).
// Single-process chromium only (fits the worker cgroup); one browser, one
// reused page. No docker containers are created here.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';
import { randomBytes, createHash } from 'node:crypto';
import { launchBrowser } from './playwright.mjs';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, extname, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outIdx = process.argv.indexOf('--out');
const out = resolve(outIdx >= 0 && process.argv[outIdx + 1] ? process.argv[outIdx + 1] : resolve(root, '.local/phone-ui'));
await mkdir(out, { recursive: true, mode: 0o700 });

const LAUNCH_ARGS = ['--single-process', '--no-zygote', '--renderer-process-limit=1', '--in-process-gpu', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--disable-breakpad', '--disable-crash-reporter', '--disable-site-isolation-trials', '--disable-features=site-per-process', '--js-flags=--thread-pool-size=1', '--num-raster-threads=1', '--disable-threaded-animation', '--disable-threaded-scrolling'];

const checks = [], shots = [];
const key = randomBytes(32).toString('hex');
const secret = randomBytes(32).toString('hex');
const adminToken = randomBytes(32).toString('hex');
const bundle = await build({ entryPoints: [resolve(root, 'test/existing-ui-worker.ts')], bundle: true, write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.woff2': 'font/woff2' };
const mf = new Miniflare(convertV4MiniflareOptions({
  host: '127.0.0.1', port: 0, log: new Log(LogLevel.ERROR), workers: [{
    name: 'phone-ui-local', script: bundle.outputFiles[0].text, modules: true, compatibilityDate: '2024-04-05', compatibilityFlags: ['nodejs_compat'],
    durableObjects: { HUB: { className: 'Hub', useSQLite: true } },
    bindings: { SESSION_SECRET: secret, HUB_ADMINS: 'admin@fake.local', LOCAL_FIXTURE_KEY: key, HUB_ADMIN_TOKEN_SHA256: createHash('sha256').update(adminToken).digest('hex'), AUTH_BASE_URL: 'https://auth.fake.local', AUTH_CALLBACK_URL: 'https://auth.fake.local/auth/callback', AUTH_CLIENT_ID: 'local-only' },
    serviceBindings: { ASSETS: async r => { const f = resolve(root, 'public', '.' + new URL(r.url).pathname); assert.ok(f.startsWith(resolve(root, 'public') + '/')); try { return new Response(await readFile(f), { headers: { 'Content-Type': mime[extname(f)] || 'application/octet-stream' } }); } catch { return new Response(null, { status: 404 }); } } },
  }],
}));
let browser;
try {
  const base = (await mf.ready).origin;
  const fixture = async (action, code) => {
    const r = await fetch(base + '/__ui_fixture', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify({ action, code }) });
    assert.equal(r.status, 200); return r.json();
  };
  const human = await fixture('seed');
  const api = (path, method = 'GET', body) => fetch(base + path, { method, headers: { Cookie: human.cookie, 'X-CSRF-Token': key, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), redirect: 'manual' });
  const src = { type: 'unknown', reference: null, timestamp: null };
  const mk = async (title, extra = {}) => {
    const r = await api('/api/intake/tasks', 'POST', { key: 'phone-' + randomBytes(6).toString('hex'), project: 'phone', title, text: 'Phone fixture request', source: src, acceptance_criteria: 'AC: phone fixture must render.', ...extra });
    assert.equal(r.status, 200); return (await r.json()).task;
  };
  const t1 = await mk('Phone list task alpha');
  const t2 = await mk('Phone approval task beta');
  let v = t2.control.version;
  const ev = async (id, version, payload) => {
    const r = await api('/api/intake/tasks/' + id + '/events', 'POST', { key: 'phone-' + randomBytes(6).toString('hex'), expected_version: version, ...payload });
    assert.equal(r.status, 200); return (await r.json()).task;
  };
  const afterFlag = await ev(t2.id, v, { event: { kind: 'pending_approval', text: 'Needs operator eyes on phone' } });
  const pendingRef = afterFlag.unresolved.find(e => e.kind === 'pending_approval').id;
  const pendingVersion = afterFlag.control.version;
  checks.push('Seeded 2 intake tasks via the real API (one pending approval)');

  browser = await launchBrowser({ args: LAUNCH_ARGS });
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', e => errors.push('browser script error: ' + (e && e.message)));
  const cookie = async value => { const [name, val] = value.split('='); await context.addCookies([{ name, value: val, url: base, httpOnly: true, sameSite: 'Lax' }]); };
  const noOverflow = async (where) => {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `${where}: horizontal overflow ${overflow}px at 390px`);
  };
  const noSecrets = async () => {
    const html = await page.content(), storage = await page.evaluate(() => JSON.stringify({ ...localStorage }));
    for (const v of [key, secret, adminToken]) { assert.ok(!html.includes(v), 'secret absent from DOM'); assert.ok(!storage.includes(v), 'secret absent from storage'); }
  };
  const capture = async name => { await page.evaluate(() => document.fonts.ready); await noOverflow(name); await noSecrets(); await page.screenshot({ path: resolve(out, name + '.png') }); shots.push(name + '.png'); };

  const STATIC_PAGES = ['/', '/sessions', '/fleet', '/conversations', '/engines', '/metrics', '/intake', '/intake/' + t1.id];
  for (const theme of ['light', 'dark']) {
    await context.clearCookies();
    // Unauthenticated clean URLs stay gated (redirect), never 404.
    for (const p of STATIC_PAGES) {
      const r = await fetch(base + p, { redirect: 'manual' });
      assert.ok([200, 303].includes(r.status), `unauth ${p} gated, got ${r.status}`);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + '/auth/token');
    await page.evaluate(t => localStorage.setItem('dakit-theme', t), theme);
    await cookie(human.cookie);
    for (const p of STATIC_PAGES) {
      await page.goto(base + p);
      await page.locator('h1').first().waitFor();
      if (p === '/') await page.waitForFunction(() => /^Forgotten: \d+$/.test(document.getElementById('intake-forgotten')?.textContent || ''));
      await capture(`phone${p === '/' ? '-home' : p.replaceAll('/', '-')}-390-${theme}`);
    }
    checks.push(`Theme ${theme}: all 8 routes render at 390x844 with h1, no horizontal scroll, no secret leakage`);
  }

  // Authenticated functional pass (dark, phone width).
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => localStorage.setItem('dakit-theme', 'dark'));
  await page.goto(base + '/intake');
  await page.getByText('Phone list task alpha', { exact: false }).waitFor();
  // Filter narrows the list.
  await page.getByLabel('Filter tasks').fill('approval task beta');
  await page.getByText('Phone approval task beta', { exact: false }).waitFor();
  assert.equal(await page.locator('.intake-item').count(), 1);
  await page.getByLabel('Filter tasks').fill('');
  await capture('phone-intake-filter-390-dark');
  checks.push('Intake list filter narrows to the matching task at phone width');

  // Read-only open: the detail shows every AC2 element.
  await page.getByText('Phone list task alpha', { exact: false }).click();
  await page.waitForURL('**/intake/*');
  const detailUrl = page.url();
  assert.ok(/\/intake\/[A-Za-z0-9._:-]+$/.test(new URL(detailUrl).pathname), 'detail uses /intake/<id>');
  await page.getByText('AC: phone fixture must render.', { exact: false }).waitFor();
  for (const label of ['Task ID', 'Project', 'Status', 'Version', 'Acceptance criteria', 'Linked sessions', 'History']) {
    assert.ok((await page.locator('#main-content').textContent()).includes(label), `detail shows ${label}`);
  }
  await capture('phone-intake-detail-390-dark');
  checks.push('Intake detail shows title, status, AC, history, linked sessions at phone width');

  // Note action from the phone UI.
  await page.getByLabel('Add a note').fill('Phone note 1');
  await page.getByRole('button', { name: 'Add note', exact: true }).click();
  await page.getByText('Phone note 1', { exact: false }).waitFor();
  await capture('phone-intake-note-390-dark');
  checks.push('Intake note action works from phone width');

  // Status action from the phone UI.
  await page.getByLabel('New status').selectOption('in_progress');
  await page.getByRole('button', { name: 'Save status', exact: true }).click();
  await page.getByText('Status saved.', { exact: false }).waitFor();
  checks.push('Intake status action works from phone width');

  // Opening never started a session: no link events, no assigned sessions.
  const sessions = await page.locator('#intake-detail-sessions').textContent();
  assert.ok(sessions.includes('No linked sessions'), 'read-only open linked nothing');
  const histCount = await page.locator('.intake-history-card').count();
  assert.ok(histCount >= 3, `history holds intake+note+update, saw ${histCount}`);
  checks.push('Opening a task started no session (no linked sessions; history only from explicit actions)');

  // Approve action on the pending task.
  await page.goto(base + '/intake/' + t2.id);
  await page.locator('#intake-approve-text', { hasText: 'Needs operator eyes on phone' }).waitFor();
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await page.getByText('Approved.', { exact: false }).waitFor();
  await capture('phone-intake-approve-390-dark');
  const approved = await (await api('/api/intake/tasks/' + t2.id)).json();
  assert.ok(!approved.task.unresolved.some(e => e.id === pendingRef), 'approval resolved the exact obligation');
  assert.equal(approved.task.control.version, pendingVersion + 1, 'approval advanced exactly one version (no session side effects)');
  checks.push('Intake approve resolves the pending obligation with no session side effects');

  // Sessions page: capture/send controls exist with real ack states, never "accepted".
  // Live pass on a seeded running session: capture + send enqueue through the
  // real handler and surface their recorded states at phone width.
  const phoneSession = await fixture('phone-session');
  const sessionId = phoneSession.created;
  assert.ok(/^[0-9a-f-]{36}$/.test(sessionId), 'fixture seeded a session id');
  await page.goto(base + '/sessions.html?session=' + encodeURIComponent(sessionId));
  await page.getByRole('button', { name: 'Capture log', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Capture log', exact: true }).click();
  await page.getByText('Capture requested (recorded).', { exact: false }).waitFor();
  await page.getByLabel('Send message / instruction').fill('Phone hello');
  await page.getByRole('button', { name: 'Send to session', exact: true }).click();
  await page.getByText('Message queued (recorded).', { exact: false }).waitFor();
  const cmdBadges = await page.locator('.session-command-card .dk-badge').allTextContents();
  assert.ok(cmdBadges.includes('recorded'), `ack states surface truthfully, saw: ${cmdBadges.join(',')}`);
  assert.ok(!cmdBadges.some(t => t.toLowerCase() === 'accepted'), 'delivery never shown as accepted');
  const sessionsBody = await page.locator('#main-content').textContent();
  assert.ok(!/accepted/i.test(sessionsBody.replace(/Accepted outcomes|accepted_outcome/gi, '')), 'sessions page never shows delivery as accepted');
  await capture('phone-sessions-390-dark');
  checks.push('Sessions capture and send enqueue live at phone width with recorded ack states; delivery never shown as accepted');

  // Conversations page: composer (send) present at phone width, no accepted-delivery badge.
  await page.goto(base + '/conversations.html');
  await page.locator('h1', { hasText: 'Conversations' }).waitFor();
  const convBody = await page.locator('#main-content').textContent();
  assert.ok(!/accepted/i.test(convBody.replace(/Accepted Outcome|accepted_outcome/gi, '')), 'conversations never show delivery as accepted');
  await capture('phone-conversations-390-dark');
  checks.push('Conversations usable at phone width; delivery never shown as accepted');

  assert.deepEqual(errors, []);
  await writeFile(resolve(out, 'results.json'), JSON.stringify({ checks, screenshots: shots, apiMocks: false, pageErrors: 0, viewport: '390x844', themes: ['light', 'dark'] }, null, 2) + '\n');
  for (const c of checks) console.log('PASS ' + c);
  console.log(`PASS ${shots.length} phone screenshots; zero browser script errors`);
} finally {
  if (browser) await browser.close();
  await mf.dispose();
}
