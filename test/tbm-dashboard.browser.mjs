// TBM dashboard browser proof (AC5): mocked TBM REST + static files from disk.
// No backend changes; verifies spend-vs-pace, banked deadlines, kill-switch,
// honest quota-percent wording, CSRF, auth errors, responsive 1440/390 light/dark.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

const { chromium } = createRequire(process.env.PLAYWRIGHT_PACKAGE || '/home/alexey/git/dataops/package.json')('playwright');
const browser = await chromium.launch({
  args: ['--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--num-raster-threads=1'],
});

const NOW = Date.parse('2026-10-26T12:00:00Z');
const pacing = () => ({
  as_of: new Date(NOW).toISOString(),
  routing_factors: { codex: { pacing: 2.1, banked: 1.4, preempt: false, halted: false } },
  pools: [
    {
      pool_id: 'codex:main', provider: 'codex', account: 'main',
      total_capacity_tokens: 0, remaining_tokens: 0, headroom_percentage: 62,
      capacity_known: 0, remaining_percent: 62,
      reset_at_ms: NOW + 36 * 3600000,
      hard_floor_percent: 5, soft_reserve_percent: 20, convexity_exponent: 1.5,
      current_burn_rate_tpm: 900, target_burn_rate_tpm: 400, pacing_ratio: 2.25,
      kill_switch_active: 0, kill_switch_reason: null, last_updated_ms: NOW,
      cycle_ms: 7 * 86400000, window_type: '7d', quota_unit: 'quota_percent',
    },
    {
      pool_id: 'claude:main', provider: 'claude', account: 'main',
      total_capacity_tokens: null, remaining_tokens: null, headroom_percentage: null,
      reset_at_ms: null, kill_switch_active: 0, last_updated_ms: NOW,
      cycle_ms: null, window_type: '5h', quota_unit: 'quota_percent',
    },
  ],
});
let banked = () => ({
  as_of: new Date(NOW).toISOString(),
  tranches: [
    { tranche_id: 't-early', pool_id: 'codex:main', provider: 'codex', label: 'rollover', allocated_tokens: 50000, remaining_tokens: 40000, granted_at_ms: NOW - 86400000, expires_at_ms: NOW + 5 * 3600000, status: 'active', banked_burn_rate_tpm: 10, target_banked_burn_rate_tpm: 20, expired_unused_tokens: 0, updated_at_ms: NOW },
    { tranche_id: 't-late', pool_id: 'codex:main', provider: 'codex', label: 'bonus', allocated_tokens: 20000, remaining_tokens: 20000, granted_at_ms: NOW - 3600000, expires_at_ms: NOW + 50 * 3600000, status: 'active', banked_burn_rate_tpm: 0, target_banked_burn_rate_tpm: 5, expired_unused_tokens: 0, updated_at_ms: NOW },
  ],
});
let killState = { active: false, reason: '' };
let authStatus = 200;
let mode = 'normal';
let lastKillPost = null;
const requests = [];
await mkdir('.local/tbm-dashboard', { recursive: true, mode: 0o700 });
try {
  for (const [width, scheme] of [[1440, 'light'], [1440, 'dark'], [390, 'light'], [390, 'dark']]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: scheme });
    await context.route('**/*', async (route) => {
      const request = route.request();
      const u = new URL(request.url());
      assert.equal(u.origin, 'http://tbm.fixture', 'no external service');
      requests.push(`${request.method()} ${u.pathname}${u.search}`);
      if (u.pathname === '/auth/me') {
        if (authStatus !== 200) return route.fulfill({ status: authStatus, body: '' });
        return route.fulfill({ json: { email: 'orin@example.invalid', csrf_token: 'fixture' } });
      }
      if (u.pathname === '/auth/login') return route.fulfill({ body: 'Login', contentType: 'text/html' });
      if (u.pathname === '/api/metrics') {
        if (!u.searchParams.has('host_id')) return route.fulfill({ json: { items: [], next_cursor: null } });
        return route.fulfill({ json: { status: 'unknown' } });
      }
      if (u.pathname === '/api/quota/pacing') {
        assert.equal(request.headers()['x-csrf-token'], 'fixture');
        if (mode === 'error') return route.fulfill({ status: 503, body: '' });
        return route.fulfill({ json: pacing() });
      }
      if (u.pathname === '/api/quota/banked') {
        assert.equal(request.headers()['x-csrf-token'], 'fixture');
        if (mode === 'error') return route.fulfill({ status: 503, body: '' });
        return route.fulfill({ json: banked() });
      }
      if (u.pathname === '/api/quota/kill-switch') {
        if (request.method() === 'POST') {
          assert.equal(request.headers()['x-csrf-token'], 'fixture');
          const body = JSON.parse(request.postData() || '{}');
          assert.equal(typeof body.active, 'boolean');
          assert.equal(typeof body.reason, 'string');
          lastKillPost = body;
          killState = { active: body.active, reason: body.reason };
          return route.fulfill({ json: killState });
        }
        assert.equal(request.headers()['x-csrf-token'], 'fixture');
        if (mode === 'error') return route.fulfill({ status: 503, body: '' });
        return route.fulfill({ json: killState });
      }
      try {
        return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    killState = { active: false, reason: '' };
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto('http://tbm.fixture/metrics.html');
    await page.waitForFunction(() => document.getElementById('tbm-status').textContent.includes('TBM ·'));
    // Spend-vs-pace per pool.
    assert.equal(await page.locator('#tbm-pacing .tbm-pool').count(), 2);
    assert.equal(await page.locator('#tbm-pacing .tbm-pool svg').count(), 1, 'only the pool with reset renders a chart');
    assert.match(await page.locator('#tbm-pacing').innerText(), /codex.*main/);
    assert.match(await page.locator('#tbm-pacing').innerText(), /62\.0% quota left/);
    assert.match(await page.locator('#tbm-pacing').innerText(), /quota-percent projection/i);
    assert.match(await page.locator('#tbm-pacing').innerText(), /real token/i);
    assert.match(await page.locator('#tbm-pacing').innerText(), /window 7d/);
    assert.match(await page.locator('#tbm').innerText(), /routing factors/i);
    // Banked deadlines sorted earliest first.
    const firstRow = await page.locator('#tbm-banked tbody tr').first().innerText();
    assert.match(firstRow, /rollover/);
    assert.equal(await page.locator('#tbm-banked tbody tr').count(), 2);
    assert.match(await page.locator('#tbm-banked').innerText(), /Earliest deadline first/);
    // Kill-switch off, then activate with reason + CSRF.
    assert.match(await page.locator('#tbm-kill-status').innerText(), /off/);
    await page.locator('#tbm-kill-reason').fill('staged low-quota drill');
    await page.locator('#tbm-kill-toggle').click();
    await page.waitForFunction(() => document.getElementById('tbm-kill-status').textContent.includes('ACTIVE'));
    assert.deepEqual(lastKillPost, { active: true, reason: 'staged low-quota drill' });
    assert.match(await page.locator('#tbm-kill-status').innerText(), /staged low-quota drill/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, 'no page overflow');
    await page.screenshot({ path: `.local/tbm-dashboard/${width}-${scheme}.png`, fullPage: true });

    if (width === 1440 && scheme === 'light') {
      // Empty banked + service failure paths stay honest, never zero-filled.
      const keep = banked;
      banked = () => ({ as_of: new Date(NOW).toISOString(), tranches: [] });
      await page.locator('#tbm-refresh').click();
      await page.waitForFunction(() => document.getElementById('tbm-refresh').disabled === false);
      await page.waitForFunction(() => document.getElementById('tbm-banked').textContent.includes('No banked quota'));
      banked = keep;
      mode = 'error';
      await page.locator('#tbm-refresh').click();
      await page.waitForFunction(() => document.getElementById('tbm-status').textContent.includes('unavailable'));
      mode = 'normal';
      await page.locator('#tbm-refresh').click();
      await page.waitForFunction(() => document.getElementById('tbm-status').textContent.includes('TBM ·'));
      authStatus = 403;
      await page.locator('#tbm-refresh').click();
      await page.waitForFunction(() => document.getElementById('tbm-status').textContent.includes('denied'));
      authStatus = 200;
      await page.goto('http://tbm.fixture/metrics.html');
      await page.waitForFunction(() => document.getElementById('tbm-status').textContent.includes('TBM ·'));
      killState = { active: false, reason: '' };
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  await writeFile('.local/tbm-dashboard/results.json', JSON.stringify({ checks: ['pools', 'pace-chart', 'banked', 'kill-switch-csrf', 'empty', 'errors', 'responsive'], requests: requests.length }, null, 2) + '\n');
  console.log('PASS tbm dashboard: 4 responsive/theme renders, pace chart, banked deadlines, kill-switch CSRF, honest units, error paths.');
} finally { await browser.close(); }
