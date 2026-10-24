// UI contract only; server liveness/progress are exercised in session-stall.test.ts.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
let playwright;
for (const path of [process.env.PLAYWRIGHT_PACKAGE, resolve('node_modules/playwright/package.json'), '/home/alexey/git/dataops/package.json'].filter(Boolean)) {
  try { playwright = createRequire(path)('playwright'); break; } catch { /* use the existing local/CI browser install */ }
}
if (!playwright) throw new Error('Playwright unavailable');
const { chromium } = playwright;
process.env.TMPDIR ||= resolve('.local/tmp');
await mkdir(process.env.TMPDIR, { recursive: true });
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--num-raster-threads=1'] });
const id = '11111111-1111-4111-8111-111111111111';
const session = { id, state: 'stalled', lifecycle_state: 'running', stalled: true, mode: 'interactive', session_type: 'aplexer', engine: 'codex', workspace: '/test', name: 'stalled-test', created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
try {
  for (const width of [390, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, 'http://stall.fixture');
      if (url.pathname === '/auth/me') return route.fulfill({ json: { email: 'fixture@example.invalid', csrf_token: 'fixture' } });
      if (url.pathname === '/api/sessions/' + id) return route.fulfill({ json: session });
      if (url.pathname === '/api/sessions') return route.fulfill({ json: { sessions: [session], next_cursor: null } });
      if (url.pathname.endsWith('/commands')) return route.fulfill({ json: { commands: [{ id: 'capture', kind: 'capture', state: 'acked', ack: { text: 'Last output' }, created_at: new Date().toISOString() }] } });
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      try { return route.fulfill({ body: await readFile(resolve('public', '.' + url.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' })[extname(url.pathname)] || 'text/plain' }); }
      catch { return route.fulfill({ status: 404 }); }
    });
    const page = await context.newPage();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://stall.fixture/sessions.html?session=' + id);
    await page.waitForFunction(() => document.querySelector('#session-detail-badges')?.textContent.includes('stalled'));
    for (const button of ['session-stop-btn', 'session-capture-tail-btn', 'session-capture-screen-btn', 'session-send-btn']) {
      assert.equal(await page.locator('#' + button).isEnabled(), true, button + ' remains enabled for stalled workload');
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log('Stalled session web badge and controls passed at phone and desktop widths.');
} finally { await browser.close(); }
