// Focused Sessions landing, detail/back, and launch-dialog browser workflow.
// Run: node test/session-workflow.browser.mjs --out DIR
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';
import { randomBytes, createHash } from 'node:crypto';
import { launchBrowser } from './playwright.mjs';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outArg = process.argv.indexOf('--out');
const out = resolve(outArg >= 0 && process.argv[outArg + 1] ? process.argv[outArg + 1] : resolve(root, '.local/session-workflow'));
await mkdir(out, { recursive: true, mode: 0o700 });
const key = randomBytes(32).toString('hex'), secret = randomBytes(32).toString('hex'), token = randomBytes(32).toString('hex');
const bundle = await build({ entryPoints: [resolve(root, 'test/existing-ui-worker.ts')], bundle: true, write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers', 'node:async_hooks'] });
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.woff2': 'font/woff2' };
const mf = new Miniflare(convertV4MiniflareOptions({ host: '127.0.0.1', port: 0, log: new Log(LogLevel.ERROR), workers: [{
  name: 'session-workflow-local', script: bundle.outputFiles[0].text, modules: true, compatibilityDate: '2024-04-05', compatibilityFlags: ['nodejs_compat'],
  durableObjects: { HUB: { className: 'Hub', useSQLite: true } },
  bindings: { SESSION_SECRET: secret, HUB_ADMINS: 'admin@fake.local', LOCAL_FIXTURE_KEY: key, HUB_ADMIN_TOKEN_SHA256: createHash('sha256').update(token).digest('hex'), AUTH_BASE_URL: 'https://auth.fake.local', AUTH_CALLBACK_URL: 'https://auth.fake.local/auth/callback', AUTH_CLIENT_ID: 'local-only' },
  serviceBindings: { ASSETS: async request => { const file = resolve(root, 'public', '.' + new URL(request.url).pathname); assert.ok(file.startsWith(resolve(root, 'public') + '/')); try { return new Response(await readFile(file), { headers: { 'Content-Type': mime[extname(file)] || 'application/octet-stream' } }); } catch { return new Response(null, { status: 404 }); } } },
}] }));
let browser;
try {
  const base = (await mf.ready).origin;
  const fixture = async action => {
    const response = await fetch(base + '/__ui_fixture', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
    assert.equal(response.status, 200); return response.json();
  };
  const human = await fixture('seed');
  const session = await fixture('phone-session');
  browser = await launchBrowser({ args: ['--single-process', '--no-zygote', '--renderer-process-limit=1', '--in-process-gpu', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--disable-breakpad', '--disable-crash-reporter', '--disable-site-isolation-trials', '--disable-features=site-per-process', '--js-flags=--thread-pool-size=1', '--num-raster-threads=1'] });
  const context = await browser.newContext();
  const [cookieName, cookieValue] = human.cookie.split('=');
  await context.addCookies([{ name: cookieName, value: cookieValue, url: base, httpOnly: true, sameSite: 'Lax' }]);
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const posts = [];
  page.on('request', request => { if (request.method() === 'POST' && /\/api\/sessions\/.*\/(capture|send|stop)|\/api\/fleet\/launches/.test(request.url())) posts.push(request.url()); });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/`);
  await page.getByRole('heading', { name: 'Sessions', exact: true }).waitFor();
  await page.getByRole('button', { name: 'New session' }).first().waitFor();
  await page.locator('#overview-running-count').waitFor();
  // Capture the exact starting page markup/style from the branch base for a
  // visual comparison; runtime assets and API remain local to this test.
  const beforeHtml = execFileSync('git', ['show', 'origin/main:public/sessions.html'], { cwd: root, encoding: 'utf8' });
  const beforeCss = execFileSync('git', ['show', 'origin/main:public/sessions.css'], { cwd: root, encoding: 'utf8' });
  await page.route('**/sessions.html*', route => route.fulfill({ status: 200, contentType: 'text/html', body: beforeHtml }));
  await page.route('**/sessions.css*', route => route.fulfill({ status: 200, contentType: 'text/css', body: beforeCss }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/sessions.html`);
  await page.locator('.sessions-row').first().waitFor();
  await page.screenshot({ path: resolve(out, 'before-1440x900.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.sessions-card-item').first().waitFor();
  await page.screenshot({ path: resolve(out, 'before-390x844.png') });
  await page.unroute('**/sessions.html*'); await page.unroute('**/sessions.css*');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/sessions.html?view=running&q=demo`);
  await page.locator('.sessions-row').first().waitFor();
  assert.equal(await page.locator('#sessions-filter').inputValue(), 'demo');
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: resolve(out, 'after-1440x900.png') });
  await page.locator('.sessions-row').first().click();
  await page.locator('#session-detail-title').waitFor();
  assert.ok(await page.locator('#session-detail-section').isVisible());
  assert.equal(posts.length, 0, 'opening a session must not enqueue a command or launch');
  await page.getByRole('button', { name: 'Back to sessions' }).click();
  assert.equal(new URL(page.url()).searchParams.get('q'), 'demo', 'detail/back preserves filter');
  await page.getByRole('button', { name: 'New session' }).click();
  const dialog = page.locator('.session-launch-dialog'); await dialog.waitFor();
  assert.equal(await dialog.evaluate(el => el.open), true);
  assert.equal(await dialog.locator('[name="workspace"]').getAttribute('required'), '');
  assert.equal(await dialog.locator('[name="summary"]').getAttribute('required'), '');
  assert.equal(await dialog.locator('[name="prompt"]').getAttribute('required'), '');
  await dialog.locator('[name="mode"]').selectOption('interactive');
  assert.equal(await dialog.locator('[name="machine"]').getAttribute('required'), '', 'interactive mode requires a machine');
  assert.equal(await dialog.locator('[name="prompt"]').isDisabled(), true, 'interactive mode cannot submit a headless goal');
  await dialog.locator('[name="mode"]').selectOption('headless');
  assert.equal(posts.length, 0, 'opening the launch dialog must not create a session');
  await dialog.screenshot({ path: resolve(out, 'after-launch-dialog-1440x900.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.screenshot({ path: resolve(out, 'after-launch-dialog-390x844.png') });
  await dialog.getByRole('button', { name: 'Close' }).click();
  await page.locator('.sessions-attention').screenshot({ path: resolve(out, 'after-attention-390x844.png') });
  assert.deepEqual(errors, [], 'no browser runtime errors');
  console.log(JSON.stringify({ status: 'passed', session, checks: ['sessions first page renders', 'filter remains in URL after detail/back', 'detail opening queues no capture/send/stop', 'launch dialog opens and required inputs are present without launching', 'desktop and mobile screenshots saved'] }));
  await context.close();
} finally { if (browser) await browser.close(); await mf.dispose(); }
