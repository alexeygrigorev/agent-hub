import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, extname } from 'node:path';

// Usage history charts: per-window quota lines with reset markers and native
// token lines, 24h/7d/30d selector, real line charts with live data.
let playwright;
for (const candidate of [process.env.PLAYWRIGHT_PACKAGE, resolve('node_modules/playwright/package.json'), '/home/alexey/git/dataops/package.json', resolve('package.json')].filter(Boolean)) {
  try { playwright = createRequire(candidate)('playwright'); break; } catch { /* try the next existing browser harness */ }
}
if (!playwright) throw Error('Playwright unavailable; install the existing CI browser harness or set PLAYWRIGHT_PACKAGE.');
const { chromium } = playwright;
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || (existsSync(chromium.executablePath()) ? chromium.executablePath() : '/home/alexey/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell'), args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--num-raster-threads=1'] });

const end = Date.parse('2026-10-26T01:00:00Z'), start = end - 86400000;
const points = (values, resetEvery = 0) => values.map((value, i) => ({
  at: start + i * 3600000,
  value,
  observed_at: value === null ? null : new Date(start + i * 3600000).toISOString(),
}));
const resetPoints = (resetAt) => Array.from({ length: 24 }, (_, i) => ({
  at: start + i * 3600000,
  value: i < 12 ? resetAt[0] : resetAt[1],
  observed_at: new Date(start + i * 3600000).toISOString(),
}));
const series = [
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: '7d', points: points([75, 74, null, 73, 72, 71, 70, 69, 68, 67, 66, 65, 64, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53]) },
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: '7d__reset_at', points: resetPoints([Date.parse('2026-11-01T06:10:58Z'), Date.parse('2026-11-08T06:10:58Z')]) },
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: 'monthly', points: points(Array(24).fill(null)) },
  { kind: 'tokens', entity: 'codex / shared-codex / agent-hub', machine_id: 'hetzner', metric: 'input_tokens', points: points([100, 200, 0, 150, 300, 250, 0, 0, 400, 100, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) },
  { kind: 'tokens', entity: 'codex / shared-codex / agent-hub', machine_id: 'hetzner', metric: 'output_tokens', points: points([10, 20, 0, 15, 30, 25, 0, 0, 40, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) },
];
const sources = [
  { engine: 'codex', supported: true, detail: 'codex rollout token_usage_record (shared-codex and orin homes)' },
  { engine: 'gemini', supported: false, detail: 'no native per-session token log on the host; quota percentages only' },
];
await mkdir('.local/usage-history', { recursive: true });
try {
  for (const [width, theme] of [[1440, 'light'], [390, 'light']]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme, hasTouch: width === 390 });
    let range = '24h';
    await context.route('**/*', async route => {
      const u = new URL(route.request().url());
      assert.equal(u.origin, 'http://usage.fixture');
      if (u.pathname === '/auth/me') return route.fulfill({ json: { email: 'fixture@example.invalid', csrf_token: 'fixture' } });
      if (u.pathname === '/api/monitoring/history') {
        range = u.searchParams.get('range');
        return route.fulfill({ json: { range, start: new Date(start).toISOString(), end: new Date(end).toISOString(), retention_days: 32, series, note: 'Gaps are unknown.' } });
      }
      if (u.pathname === '/api/monitoring/tokens') return route.fulfill({ json: { schema_version: '1.0', sources } });
      if (u.pathname === '/api/metrics') {
        if (!u.searchParams.has('host_id')) return route.fulfill({ json: { items: [{ host_id: 'hetzner' }], next_cursor: null } });
        return route.fulfill({ json: { status: 'unknown' } });
      }
      if (u.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
      try { return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' })[extname(u.pathname)] || 'text/plain' }); }
      catch { return route.fulfill({ status: 404 }); }
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://usage.fixture/metrics.html#usage-history');
    await page.waitForSelector('#usage-quota-history svg');
    // One line per window: 7d only (monthly is fully null and leaves no trace).
    assert.match(await page.locator('#usage-quota-history').innerText(), /codex\/openai shared-codex/);
    assert.equal(await page.locator('#usage-quota-history svg').count(), 1);
    assert.equal(await page.locator('#usage-quota-history .metrics-line').count(), 1);
    // Reset marker drawn where the reset time changed.
    assert.equal(await page.locator('#usage-quota-history line.metrics-reset').count(), 1);
    assert.match(await page.locator('#usage-quota-history').innerText(), /reset/);
    // Measured zero is plotted; gaps break the line.
    const d = await page.locator('#usage-token-history .metrics-line').first().getAttribute('d');
    assert.ok(d.includes('L'), 'real token LINE chart');
    assert.ok(await page.locator('#usage-token-history [data-value="0"]').count() > 0, 'measured zero tokens shown');
    assert.equal(await page.locator('#usage-token-history svg').count(), 1);
    // Per-engine provenance instead of a blanket Unknown.
    assert.match(await page.locator('#usage-token-sources').innerText(), /codex: native history/);
    assert.match(await page.locator('#usage-token-sources').innerText(), /gemini: not collected/);
    // Range selector offers 24h/7d/30d and refetches.
    for (const value of ['7d', '30d', '24h']) {
      await page.getByLabel('Usage history range', { exact: true }).selectOption(value);
      await page.waitForFunction(v => document.querySelector('#usage-history [data-usage-note]').textContent.startsWith(v + ' ·'), value);
      assert.equal(range, value);
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log('PASS usage history: per-window quota lines, reset markers, token lines, known zero, per-engine sources, 24h/7d/30d.');
} finally { await browser.close(); }
