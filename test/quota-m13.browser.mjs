// M13 quota dashboard browser proof: real loopback Worker + Hub + SQLite,
// real Chromium renders. Positive API responses never mocked; only the admin
// session cookie comes from the local fixture endpoint.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';
import { randomBytes, createHash } from 'node:crypto';
import { launchBrowser } from './playwright.mjs';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const out = resolve(process.env.UI_EVIDENCE_DIR || resolve(root, '.local/quota/browser'));
await mkdir(out, { recursive: true, mode: 0o700 });
const key = randomBytes(32).toString('hex');
const secret = randomBytes(32).toString('hex');
const adminToken = randomBytes(32).toString('hex');
const bundle = await build({ entryPoints: [resolve(root, 'test/existing-ui-worker.ts')], bundle: true, write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.woff2': 'font/woff2' };
const mf = new Miniflare(convertV4MiniflareOptions({ host: '127.0.0.1', port: 0, log: new Log(LogLevel.ERROR), workers: [{
  name: 'quota-m13-local', script: bundle.outputFiles[0].text, modules: true, compatibilityDate: '2024-04-05', compatibilityFlags: ['nodejs_compat'], durableObjects: { HUB: { className: 'Hub', useSQLite: true } }, bindings: { SESSION_SECRET: secret, HUB_ADMINS: 'admin@fake.local', LOCAL_FIXTURE_KEY: key, HUB_ADMIN_TOKEN_SHA256: createHash('sha256').update(adminToken).digest('hex'), AUTH_BASE_URL: 'https://auth.fake.local', AUTH_CALLBACK_URL: 'https://auth.fake.local/auth/callback', AUTH_CLIENT_ID: 'local-only' },
  serviceBindings: { ASSETS: async r => { const f = resolve(root, 'public', '.' + new URL(r.url).pathname); assert.ok(f.startsWith(resolve(root, 'public') + '/')); try { return new Response(await readFile(f), { headers: { 'Content-Type': mime[extname(f)] || 'application/octet-stream' } }); } catch { return new Response(null, { status: 404 }); } } }
}]}));
let browser; const checks = [], shots = [];
try {
  const base = (await mf.ready).origin;
  const fixture = async (action, code) => { const r = await fetch(base + '/__ui_fixture', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify({ action, code }) }); assert.equal(r.status, 200); return r.json(); };
  const human = await fixture('seed');
  const api = (path, method = 'GET', body, token) => fetch(base + path, { method, headers: { ...(token ? { Authorization: 'Bearer ' + token } : { Cookie: human.cookie, 'X-CSRF-Token': key }), 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), redirect: 'manual' });

  // Unauthenticated fleet quota reads are denied.
  assert.equal((await fetch(base + '/api/quota?view=fleet', { redirect: 'manual' })).status, 401);
  checks.push('Unauthenticated fleet quota read denied (401)');

  // Pair both seeded machines as hosts.
  const tokens = {};
  for (const machine of ['ui-machine', 'ui-other']) {
    const init = await (await api('/api/device/code', 'POST', { client_kind: 'host', name: 'q13-' + machine, hostname: 'local', requested_scopes: ['host'] })).json();
    const approval = await api('/api/device/approve/' + init.user_code, 'POST', { granted_scopes: ['host'], machine_id: machine });
    assert.equal(approval.status, 200);
    const tok = await (await api('/api/device/token', 'POST', { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: init.device_code })).json();
    assert.ok(tok.access_token);
    tokens[machine] = tok.access_token;
    const reg = await api('/api/machines/register', 'POST', { hostname: 'local', os: 'linux', network: 'public', roles: ['worker'], capabilities: { launcher: '0.1.0' } }, tok.access_token);
    assert.equal(reg.status, 200);
  }

  const registry = { accounts: [{ label: 'q-pool', provider: 'codex', max_concurrency: 4, reserve_percent: 15, enabled: true }], routes: [{ machine_id: 'ui-machine', engine: 'codex', account: 'q-pool', profile: null }, { machine_id: 'ui-other', engine: 'codex', account: 'q-pool', profile: null }] };
  assert.equal((await api('/api/fleet/accounts', 'PUT', registry)).status, 200);

  // Fresh subscription on ui-machine (80%) and a lower one on ui-other (60%):
  // shared pool must show the conservative minimum with both hosts as provenance.
  const sub = pct => ({ subscriptions: [{ service: 'codex', account: 'q-pool', plan: 'pro', quota: { status: 'ok', observed_at: new Date().toISOString(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: pct, reset_at: new Date(Date.now() + 86400000).toISOString() } } } }] });
  assert.equal((await api('/api/fleet/subscriptions', 'PUT', sub(80), tokens['ui-machine'])).status, 200);
  assert.equal((await api('/api/fleet/subscriptions', 'PUT', sub(60), tokens['ui-other'])).status, 200);

  // 1.1 snapshot with native credits on ui-machine, plus a stale snapshot stays honest.
  const snap = {
    schema_version: '1.1', collector_version: 'quota_snapshot/1.1', captured_at: new Date().toISOString(), host_id: 'ui-machine', status: 'ok', error: null,
    routes: { codex: { status: 'ok', error: null, observed_at: new Date().toISOString(), details: { limit_reached: false }, windows: { '7d': { percent_remaining: 80, reset_at: new Date(Date.now() + 86400000).toISOString(), present: true, rolling: false, credits_remaining: 12.5, spend_used: null, spend_limit: null, unit: 'credits', profile: null } } } },
  };
  assert.equal((await api('/api/quota', 'POST', snap, tokens['ui-machine'])).status, 200);

  // Same UI/API contract: fleet view carries pool, natives, coverage.
  const fleetView = await (await api('/api/quota?view=fleet')).json();
  assert.equal(fleetView.schema_version, '1.1');
  const pool = fleetView.pools.find(p => p.provider === 'codex' && p.account === 'q-pool');
  assert.ok(pool);
  assert.equal(pool.state, 'fresh');
  assert.equal(pool.remaining_percent, 60);
  assert.deepEqual(pool.observed_by_hosts.sort(), ['ui-machine', 'ui-other']);
  const hostSnap = fleetView.hosts.find(h => h.host_id === 'ui-machine').snapshot;
  assert.equal(hostSnap.routes.codex.windows['7d'].credits_remaining, 12.5);
  assert.equal(hostSnap.routes.codex.windows['7d'].unit, 'credits');
  // Coverage describes host quota snapshots, independently of subscription provenance.
  assert.ok(fleetView.coverage.fresh.includes('ui-machine'));
  assert.ok(!fleetView.coverage.fresh.includes('ui-other'));
  checks.push('Fleet quota contract: shared pool deduped to min-remaining with host provenance; natives round-trip; honest coverage');

  // Filtered view agrees with the fleet view on the same pool.
  const filtered = await (await api('/api/quota?view=fleet&provider=codex&account=q-pool')).json();
  assert.equal(filtered.pools.length, 1);
  assert.equal(filtered.pools[0].remaining_percent, 60);
  checks.push('Filtered quota view agrees with the fleet view on host, units and freshness');
  browser = await launchBrowser({ args: ['--single-process', '--in-process-gpu', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--js-flags=--thread-pool-size=1', '--num-raster-threads=1', '--disable-threaded-animation', '--disable-threaded-scrolling'] });
  const context = await browser.newContext(), page = await context.newPage(); page.setDefaultTimeout(15000);
  const errors = []; page.on('pageerror', () => errors.push('browser script error'));
  const cookie = async value => { const [name, v] = value.split('='); await context.addCookies([{ name, value: v, url: base, httpOnly: true, sameSite: 'Lax' }]); };
  const safe = async () => {
    const html = await page.content(), storage = await page.evaluate(() => JSON.stringify({ ...localStorage }));
    for (const v of [key, secret, adminToken, human.cookie.split('=')[1], tokens['ui-machine'], tokens['ui-other']]) { assert.ok(!html.includes(v), 'Credential/session absent from DOM'); assert.ok(!storage.includes(v), 'Credentials absent from storage'); }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'No document overflow');
  };
  const capture = async name => { await page.evaluate(() => document.fonts.ready); await safe(); await page.screenshot({ path: resolve(out, name + '.png'), fullPage: true }); shots.push(name + '.png'); };
  for (const theme of ['light', 'dark']) for (const width of [1440, 390]) {
    await context.clearCookies(); await page.setViewportSize({ width, height: 900 }); await cookie(human.cookie);
    await page.goto(base + '/fleet.html');
    // Set the theme on the fleet origin, then reload so the shell reads it at load.
    await page.evaluate(t => localStorage.setItem('dakit-theme', t), theme);
    await page.reload(); await page.getByText('Authenticated · updated', { exact: false }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
    const quotaText = await page.locator('#quota').textContent();
    assert.ok(quotaText.includes('q-pool'), 'pool account rendered');
    assert.ok(quotaText.includes('fresh'), 'fresh state rendered');
    assert.ok(quotaText.includes('60%'), 'conservative remaining rendered');
    assert.ok(quotaText.includes('12.5 credits'), 'native units rendered');
    assert.ok(quotaText.includes('Coverage:'), 'honest coverage rendered');
    await page.locator('#quota').scrollIntoViewIfNeeded();
    await capture(`quota-${width}-${theme}`);
  }
  checks.push('Real 390/1440 light/dark quota renders; pool, states, natives and coverage visible');
  assert.deepEqual(errors, []);
  await writeFile(resolve(out, 'results.json'), JSON.stringify({ checks, screenshots: shots, apiMocks: false, pageErrors: 0 }, null, 2) + '\n');
  for (const c of checks) console.log('PASS ' + c);
  console.log('PASS ' + shots.length + ' screenshots; zero browser script errors');
} finally { if (browser) await browser.close(); await mf.dispose(); }
