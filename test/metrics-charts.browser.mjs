import assert from 'node:assert/strict';
import { launchBrowser } from './playwright.mjs';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

// Run from the repository root. Uses the shared local browser harness, like metrics-r6.browser.mjs.
const browser = await launchBrowser({
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--num-raster-threads=1'],
});
const asOf = Date.parse('2026-10-23T12:15:00Z');
const project = (unknown, offset = 0) => ({
  unknown, coverage: unknown ? null : 0.75,
  hourly_buckets: Array.from({ length: 24 }, (_, i) => ({
    bucket_start: new Date(asOf - (24 - i) * 3600000).toISOString(),
    bucket_end: new Date(asOf - (23 - i) * 3600000).toISOString(),
    active_agents: unknown || i === 8 ? null : i === 0 ? 0 : i % 5 + offset,
    agent_hours: unknown || i === 8 ? null : i === 0 ? 0 : (i % 5 + offset) / 2,
  })),
});
const snapshot = () => ({
  host_id: 'host-A', status: 'ok', as_of: new Date(asOf).toISOString(), stale: false,
  coverage: { utilization: 'partial', usage: 'partial', accepted: 'partial' }, gaps: [{ code: 'partial_coverage' }],
  utilization: { unknown: false, projects: { 'agent-branches': project(false), 'agent-dashboard': project(true), unattributed: project(false, 2) } },
  usage: { unknown: false, projects: {
    'agent-branches': { unknown: false, total_input_tokens: 12000, total_output_tokens: 600, has_unknown_tokens: true },
    unattributed: { unknown: true, total_input_tokens: null, total_output_tokens: null },
  } },
  accepted: { unknown: false, accepted_outcomes: 3, task_count: 7, commit_count: 9, review_count: 4, projects: {
    'agent-branches': { unknown: false, accepted_outcomes: 3 }, unattributed: { unknown: true, accepted_outcomes: null },
  } },
});
let data = snapshot();
let authStatus = 200;
let logout = false;
let mode = 'normal';
const requests = [];
await mkdir('.local/metrics-charts', { recursive: true });
try {
  for (const [width, scheme] of [[1440, 'light'], [1440, 'dark'], [390, 'light'], [390, 'dark']]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: scheme });
    await context.route('**/*', async route => {
      const request = route.request();
      const u = new URL(request.url());
      assert.equal(u.origin, 'http://metrics.fixture', 'no external chart service');
      requests.push(u.pathname);
      if (u.pathname === '/auth/me') return route.fulfill({ status: authStatus, json: authStatus === 200 ? { email: 'orin@example.invalid', csrf_token: 'fixture' } : {} });
      if (u.pathname === '/auth/logout') {
        assert.equal(request.method(), 'POST');
        assert.equal(request.headers()['x-csrf-token'], 'fixture');
        logout = true;
        return route.fulfill({ status: 200, body: '' });
      }
      if (u.pathname === '/auth/login') return route.fulfill({ body: 'Login', contentType: 'text/html' });
      if (u.pathname === '/api/metrics') {
        assert.equal(request.headers()['x-csrf-token'], 'fixture');
        assert.equal(request.method(), 'GET');
        if (mode === 'error') return route.fulfill({ status: 503, body: '' });
        if (!u.searchParams.has('host_id')) return route.fulfill({ json: { items: [{ host_id: 'host-A' }], next_cursor: null } });
        return route.fulfill({ json: mode === 'unavailable' ? { status: 'unknown' } : data });
      }
      try {
        return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://metrics.fixture/metrics.html');
    await page.waitForFunction(() => document.getElementById('status').textContent.includes('Authenticated'));
    assert.equal(await page.locator('#activity-charts svg').count(), 2);
    assert.equal(await page.locator('#activity-charts .metrics-point').count(), 92, 'only the 23 known buckets in each of two series, on each chart');
    assert.equal(await page.locator('#activity-charts [data-project="agent-dashboard"] .metrics-point').count(), 0);
    assert.equal(await page.locator('#activity-charts [data-hour="8"]').count(), 0, 'unknown bucket has no zero point');
    assert.equal(await page.locator('#activity-charts [data-hour="0"][data-value="0"]').count(), 4, 'reported zero is plotted');
    const path = await page.locator('#activity-charts path.metrics-line').first().getAttribute('d');
    assert.equal((path.match(/M/g) || []).length, 2, 'line breaks at unknown bucket');
    assert.match(await page.locator('#activity-charts').innerText(), /partial.*observed portion/);
    assert.match(await page.locator('#activity-charts').innerText(), /unattributed/);
    assert.equal(await page.locator('#usage-chart [data-value]').count(), 2);
    assert.equal(await page.locator('#usage-chart .metrics-bar-unknown').count(), 2);
    assert.match(await page.locator('#usage-chart').innerText(), /12,000|12.000/);
    assert.match(await page.locator('#usage-chart').innerText(), /Partial token coverage/);
    assert.equal(await page.locator('#accepted-chart [data-value="3"]').count(), 1);
    assert.equal(await page.locator('#accepted-chart .metrics-bar-unknown').count(), 1);
    await page.locator('#activity-charts .metrics-point').first().focus();
    assert.match(await page.locator('.metrics-point-detail').first().innerText(), /2026-10-22 12:15 UTC.*Active agents: 0/);
    await page.getByLabel('Chart project', { exact: true }).selectOption('unattributed');
    assert.equal(await page.locator('#activity-charts svg [data-project]').count(), 2);
    await page.getByLabel('Chart project', { exact: true }).selectOption('*');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, 'no page overflow');
    await page.screenshot({ path: `.local/metrics-charts/${width}-${scheme}.png`, fullPage: true });

    if (width === 1440 && scheme === 'light') {
      // Partial data and stale labels survive refresh; all-unknown never plots a zero.
      data.stale = true;
      await page.locator('#metrics-refresh').click();
      await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
      assert.match(await page.locator('#activity-charts').innerText(), /stale snapshot/);
      data.utilization.unknown = true;
      data.usage.unknown = true;
      data.accepted.unknown = true;
      await page.locator('#metrics-refresh').click();
      await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
      assert.equal(await page.locator('#activity-charts .metrics-point').count(), 0);
      assert.equal(await page.locator('#usage-chart [data-value], #accepted-chart [data-value]').count(), 0);
      assert.match(await page.locator('#activity-charts').innerText(), /Unknown/);
      // Switching to unavailable/error/expired must clear old charts.
      mode = 'unavailable';
      await page.locator('#metrics-refresh').click();
      await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
      assert.equal(await page.locator('#activity-charts svg').count(), 0);
      mode = 'error';
      await page.locator('#metrics-refresh').click();
      await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
      assert.match(await page.locator('#status').innerText(), /unavailable|fail/i);
      mode = 'normal'; data = snapshot();
      authStatus = 403;
      await page.locator('#metrics-refresh').click();
      await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
      assert.equal(await page.locator('#activity-charts svg').count(), 0);
      assert.match(await page.locator('#status').innerText(), /denied/i);
      authStatus = 401;
      await page.locator('#metrics-refresh').click();
      await page.waitForURL('**/auth/login');
      authStatus = 200;
      await page.goto('http://metrics.fixture/metrics.html');
      await page.waitForFunction(() => document.getElementById('status').textContent.includes('Authenticated'));
      await page.locator('#sign-out').dispatchEvent('click');
      await page.waitForURL('**/auth/login');
      assert.equal(logout, true);
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log('PASS metrics charts: 4 responsive/theme renders, UTC gaps, known zero, unattributed, coverage, totals, keyboard detail, unavailable, auth errors and CSRF logout.');
} finally { await browser.close(); }
