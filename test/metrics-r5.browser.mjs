import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

// M9 R5 browser probe: delayed detail responses (success AND error) for a
// superseded selection must never overwrite the current selection's
// label/status/data. Same-host refresh during a pending detail stays coherent.
const { chromium } = createRequire('/home/alexey/git/dataops/package.json')('playwright');
const browser = await chromium.launch({
  executablePath: '/home/alexey/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
    '--num-raster-threads=1', '--disable-threaded-animation', '--disable-threaded-scrolling'],
});

const now = new Date(Date.now() - 3600000).toISOString();
const tokens = { 'host-B': 222, 'host-C': 333, 'host-A': 111 };
const snapshot = id => ({
  host_id: id, as_of: now, status: 'ok', stale: false, coverage: {}, gaps: [],
  utilization: { unknown: true, projects: {} },
  usage: { unknown: false, projects: { 'agent-branches': { unknown: false, total_input_tokens: tokens[id] ?? 0, total_output_tokens: 0 } } },
  accepted: { unknown: true, projects: {} },
});

// Held-request control per host id: route signals arrival, then waits gate.
function hold() {
  let arrived;
  let release;
  const arrivedGate = new Promise(r => { arrived = r; });
  const gate = new Promise(r => { release = r; });
  return { arrivedGate, arrived, gate, release };
}
const held = new Map();
const behavior = new Map(); // id -> 'ok' | 'error500'

async function runCase(name, setup) {
  const context = await browser.newContext();
  await context.route('**/*', async route => {
    const u = new URL(route.request().url());
    assert.equal(u.origin, 'http://metrics.fixture');
    if (u.pathname === '/auth/me') return route.fulfill({ json: { email: 'synthetic@example.invalid', csrf_token: 'synthetic' } });
    if (u.pathname === '/api/metrics') {
      const id = u.searchParams.get('host_id');
      if (!id) return route.fulfill({ json: { items: ['host-A', 'host-B', 'host-C'].map(host_id => ({ host_id })), next_cursor: null } });
      if (held.has(id)) {
        const h = held.get(id);
        held.delete(id);
        h.arrived();
        await h.gate;
      }
      if (behavior.get(id) === 'error500') return route.fulfill({ status: 500, body: '' });
      return route.fulfill({ json: snapshot(id) });
    }
    try {
      return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' });
    } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  const observations = [];
  try {
    await page.goto('http://metrics.fixture/metrics.html');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-A');
    await setup(page);
    const result = {
      selected: await page.locator('#metrics-host').inputValue(),
      identity: await page.locator('#metrics-host-id').innerText(),
      status: await page.locator('#status').innerText(),
      usage: await page.locator('#usage').innerText(),
    };
    observations.push({ case: name, ...result });
    assert.equal(result.selected, 'host-C', name + ': selection');
    assert.equal(result.identity, 'Host host-C', name + ': label');
    assert.match(result.usage, /333/, name + ': data');
    assert.doesNotMatch(result.usage, /222/, name + ': stale data absent');
  } finally {
    for (const [, h] of held) h.release();
    held.clear();
    await context.close();
  }
  return observations;
}

const all = [];
// Case 1: B detail slow success, C fast — released B must not overwrite C.
{
  const h = hold();
  held.set('host-B', h);
  all.push(...await runCase('slow-success', async page => {
    await page.locator('#metrics-host').selectOption('host-B');
    await h.arrivedGate;
    await page.locator('#metrics-host').selectOption('host-C');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-C');
    h.release();
    await page.waitForFunction(() => document.getElementById('usage').textContent.includes('333'));
    await page.waitForTimeout(400);
  }));
}
// Case 2: B detail slow error AFTER C committed — stale error must not clobber C.
{
  const h = hold();
  held.set('host-B', h);
  behavior.set('host-B', 'error500');
  try {
    all.push(...await runCase('slow-error', async page => {
      await page.locator('#metrics-host').selectOption('host-B');
      await h.arrivedGate;
      await page.locator('#metrics-host').selectOption('host-C');
      await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-C');
      await page.waitForFunction(() => document.getElementById('usage').textContent.includes('333'));
      h.release();
      await page.waitForTimeout(400);
    }));
  } finally {
    behavior.delete('host-B');
  }
}
// Case 3: same-host refresh during a pending C detail — selection stays C with C data.
{
  const h = hold();
  held.set('host-C', h);
  all.push(...await runCase('samehost-refresh', async page => {
    await page.locator('#metrics-host').selectOption('host-C');
    await h.arrivedGate;
    await page.locator('#metrics-refresh').click();
    h.release();
    await page.waitForFunction(() => document.getElementById('usage').textContent.includes('333'));
    await page.waitForTimeout(400);
  }));
}

await writeFile('.local/metrics-r5/ui-race-results.json', JSON.stringify(all, null, 2));
console.log(JSON.stringify(all));

// Render matrix: 1440/390 viewports x light/dark schemes. Selected host
// identity stays visible, controls use official dakit, no document overflow.
const matrix = [];
for (const [width, height, scheme, tag] of [[1440, 900, 'light', '1440-light'], [1440, 900, 'dark', '1440-dark'], [390, 844, 'light', '390-light'], [390, 844, 'dark', '390-dark']]) {
  const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
  await context.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.pathname === '/auth/me') return route.fulfill({ json: { email: 'synthetic@example.invalid', csrf_token: 'synthetic' } });
    if (u.pathname === '/api/metrics') {
      const id = u.searchParams.get('host_id');
      if (!id) return route.fulfill({ json: { items: ['host-A', 'host-B', 'host-C'].map(host_id => ({ host_id })), next_cursor: null } });
      return route.fulfill({ json: snapshot(id) });
    }
    try {
      return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' });
    } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  try {
    await page.goto('http://metrics.fixture/metrics.html');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-A');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    const identity = await page.locator('#metrics-host-id').innerText();
    assert.equal(identity, 'Host host-A', tag + ': identity');
    assert.equal(overflow, false, tag + ': no overflow');
    await page.screenshot({ path: `.local/metrics-r5/ui-${tag}.png` });
    matrix.push({ case: 'render-' + tag, identity, overflow });
  } finally {
    await context.close();
  }
}
await writeFile('.local/metrics-r5/ui-render-results.json', JSON.stringify(matrix, null, 2));
console.log(JSON.stringify(matrix));
await browser.close();
