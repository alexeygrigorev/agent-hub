import assert from 'node:assert/strict';
import { launchBrowser } from './playwright.mjs';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

// M9 UI R6 browser probe (NEW, own private area .local/metrics-ui-r6/).
// Preserves 3 author R5 races + adds 2 pagination races + host-disappearance
// boundary. Legit fixture change vs test/metrics-r5.browser.mjs: output paths
// moved from `.local/metrics-r5/` to `.local/metrics-ui-r6/`; old tracked test
// NEVER edited. Asserts exact C identity/data/status + API routing.
const OUT = '.local/metrics-ui-r6/r6-results.json';
const RENDER_OUT = '.local/metrics-ui-r6/r6-render-results.json';
const browser = await launchBrowser({
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

function hold() {
  let arrived;
  let release;
  const arrivedGate = new Promise(r => { arrived = r; });
  const gate = new Promise(r => { release = r; });
  return { arrivedGate, arrived, gate, release };
}
const held = new Map();
const behavior = new Map();

async function runDetailCase(name, setup) {
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
      usage: await page.locator('#usage').textContent(),
    };
    observations.push({ case: name, ...result });
    assert.equal(result.selected, 'host-C', name + ': selection');
    assert.equal(result.identity, 'Host host-C', name + ': label');
    assert.match(result.status, /host-C/, name + ': status routes to C');
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
// R5-1: B slow success, C fast — released B must not overwrite C.
{
  const h = hold();
  held.set('host-B', h);
  all.push(...await runDetailCase('r5-slow-success', async page => {
    await page.locator('#metrics-host').selectOption('host-B');
    await h.arrivedGate;
    await page.locator('#metrics-host').selectOption('host-C');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-C');
    h.release();
    await page.waitForFunction(() => document.getElementById('usage').textContent.includes('333'));
    await page.waitForTimeout(400);
  }));
}
// R5-2: B slow error AFTER C committed — stale error must not clobber C.
{
  const h = hold();
  held.set('host-B', h);
  behavior.set('host-B', 'error500');
  try {
    all.push(...await runDetailCase('r5-slow-error', async page => {
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
// R5-3: same-host refresh during a pending C detail — selection stays C with C data.
{
  const h = hold();
  held.set('host-C', h);
  all.push(...await runDetailCase('r5-samehost-refresh', async page => {
    await page.locator('#metrics-host').selectOption('host-C');
    await h.arrivedGate;
    await page.locator('#metrics-refresh').click();
    h.release();
    await page.waitForFunction(() => document.getElementById('usage').textContent.includes('333'));
    await page.waitForTimeout(400);
  }));
}

// R6 pagination races: refresh starts while B selected, page1=[A] excludes B,
// page2 pending; user selects C (C333 loads); then old page2 releases.
async function runPaginationCase(failure) {
  const context = await browser.newContext();
  let refreshing = false, release, arrived;
  const gate = new Promise(r => { release = r; });
  const arrival = new Promise(r => { arrived = r; });
  const requests = [];
  await context.route('**/*', async route => {
    const u = new URL(route.request().url());
    assert.equal(u.origin, 'http://metrics.fixture');
    if (u.pathname === '/auth/me') return route.fulfill({ json: { email: 'synthetic@example.invalid', csrf_token: 'synthetic' } });
    if (u.pathname === '/api/metrics') {
      requests.push(u.pathname + u.search);
      const id = u.searchParams.get('host_id');
      if (id) return route.fulfill({ json: { host_id: id, as_of: '2026-10-16T12:00:00.000Z', status: 'ok', stale: false, coverage: {}, gaps: [], utilization: { unknown: true, projects: {} }, usage: { unknown: false, projects: { 'agent-branches': { unknown: false, total_input_tokens: id === 'host-C' ? 333 : 222, total_output_tokens: 0 } } }, accepted: { unknown: true, projects: {} } } });
      if (!refreshing) return route.fulfill({ json: { items: ['host-A', 'host-B', 'host-C'].map(host_id => ({ host_id })), next_cursor: null } });
      if (!u.searchParams.has('after')) return route.fulfill({ json: { items: [{ host_id: 'host-A' }], next_cursor: 'host-A' } });
      arrived();
      await gate;
      if (failure) return route.fulfill({ status: 503, json: { error: 'synthetic-service-failure' } });
      return route.fulfill({ json: { items: ['host-B', 'host-C'].map(host_id => ({ host_id })), next_cursor: null } });
    }
    try { return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' }); } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  try {
    await page.goto('http://metrics.fixture/metrics.html');
    await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
    await page.locator('#metrics-host').selectOption('host-B');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-B');
    refreshing = true;
    await page.locator('#metrics-refresh').click();
    await arrival;
    await page.locator('#metrics-host').selectOption('host-C');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-C');
    release();
    await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
    await page.waitForTimeout(100);
    const after = {
      selected: await page.locator('#metrics-host').inputValue(),
      identity: await page.locator('#metrics-host-id').innerText(),
      status: await page.locator('#status').innerText(),
      usage: await page.locator('#usage').textContent(),
    };
    const name = failure ? 'r6-stale-pagination-error' : 'r6-stale-pagination-success';
    all.push({ case: name, ...after, requests });
    assert.equal(after.selected, 'host-C', name + ': selection');
    assert.equal(after.identity, 'Host host-C', name + ': label');
    assert.match(after.status, /host-C/, name + ': status routes to C');
    assert.match(after.usage, /333/, name + ': data');
    assert.doesNotMatch(after.usage, /222/, name + ': stale B absent');
    if (failure) {
      // Stale 503 must not route a new B fetch nor clear C.
      assert.ok(!requests.slice(requests.indexOf('/api/metrics?host_id=host-C') + 1).includes('/api/metrics?host_id=host-B'), name + ': no stale B refetch after C');
    } else {
      // Success must reconcile to live C, not stale B.
      assert.ok(requests[requests.length - 1] === '/api/metrics?host_id=host-C', name + ': final detail routes to C, got ' + requests[requests.length - 1]);
    }
  } finally {
    try { release(); } catch {}
    await context.close();
  }
}
await runPaginationCase(false);
await runPaginationCase(true);

// R6 boundary: host disappearance — refresh returns empty while B selected;
// genuinely selected B stays (unavailable), no silent default to A.
{
  const context = await browser.newContext();
  let emptied = false;
  const requests = [];
  await context.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.pathname === '/auth/me') return route.fulfill({ json: { email: 'synthetic@example.invalid', csrf_token: 'synthetic' } });
    if (u.pathname === '/api/metrics') {
      requests.push(u.pathname + u.search);
      const id = u.searchParams.get('host_id');
      if (id) {
        if (emptied) return route.fulfill({ status: 404, json: { status: 'unknown', error: 'unavailable' } });
        return route.fulfill({ json: snapshot(id) });
      }
      if (!emptied) return route.fulfill({ json: { items: ['host-A', 'host-B', 'host-C'].map(host_id => ({ host_id })), next_cursor: null } });
      return route.fulfill({ json: { items: [], next_cursor: null } });
    }
    try { return route.fulfill({ body: await readFile(resolve('public', '.' + u.pathname)), contentType: ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[extname(u.pathname)] || 'text/plain' }); } catch { return route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  try {
    await page.goto('http://metrics.fixture/metrics.html');
    await page.waitForFunction(() => document.getElementById('metrics-host-id').textContent === 'Host host-A');
    await page.locator('#metrics-host').selectOption('host-B');
    await page.waitForFunction(() => document.getElementById('usage').textContent.includes('222'));
    emptied = true;
    await page.locator('#metrics-refresh').click();
    await page.waitForFunction(() => !document.getElementById('metrics-refresh').disabled);
    await page.waitForTimeout(200);
    const after = {
      selected: await page.locator('#metrics-host').inputValue(),
      identity: await page.locator('#metrics-host-id').innerText(),
      status: await page.locator('#status').innerText(),
      usage: await page.locator('#usage').textContent(),
    };
    all.push({ case: 'r6-host-disappearance', ...after, requests });
    assert.equal(after.selected, 'host-B', 'disappearance: selection preserved');
    assert.match(after.identity, /host-B/, 'disappearance: identity preserves B');
    assert.match(after.identity, /no longer available|unavailable/i, 'disappearance: unavailable label');
    assert.doesNotMatch(after.selected, /host-A/, 'disappearance: no silent default to A');
  } finally {
    await context.close();
  }
}

await mkdir('.local/metrics-ui-r6', { recursive: true });
await writeFile(OUT, JSON.stringify(all, null, 2));
console.log(JSON.stringify(all));

// Render matrix: 1440/390 x light/dark. Real screenshots, no fake evidence.
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
    await page.screenshot({ path: `.local/metrics-ui-r6/ui-${tag}.png` });
    matrix.push({ case: 'render-' + tag, identity, overflow });
  } finally {
    await context.close();
  }
}
await writeFile(RENDER_OUT, JSON.stringify(matrix, null, 2));
console.log(JSON.stringify(matrix));
await browser.close();
