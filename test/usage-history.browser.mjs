import assert from 'node:assert/strict';
import { createRequire, stripTypeScriptTypes } from 'node:module';
import { DatabaseSync } from 'node:sqlite';
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
  { kind: 'quota_window', entity: 'shared-codex-old', machine_id: '', metric: '5h', points: points([90, 88, 85, 80, 79, 78, 77, 76, 75, 74, 73, 72, 71, 70, 69, 68, 67, 66, 65, 64, 63, 62, 61, 60]) },
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: '7d', points: points([75, 74, null, 73, 72, 71, 70, 69, 68, 67, 66, 65, 64, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53]) },
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: '7d__reset_at', points: resetPoints([Date.parse('2026-11-01T06:10:58Z'), Date.parse('2026-11-08T06:10:58Z')]) },
  { kind: 'quota_window', entity: 'shared-codex', machine_id: '', metric: 'monthly', points: points(Array(24).fill(null)) },
  { kind: 'tokens', entity: 'codex / shared-codex-old / agent-hub', machine_id: 'hetzner', metric: 'input_tokens', points: points([100, 200, 0, 150, 300, 250, 0, 0, 400, 100, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) },
  { kind: 'tokens', entity: 'codex / shared-codex-old / agent-hub', machine_id: 'hetzner', metric: 'output_tokens', points: points([10, 20, 0, 15, 30, 25, 0, 0, 40, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) },
];
// Feed the renderer genuine query() results, including its null grid and
// correction timestamps. This catches hourly totals accidentally placed on a
// five-minute gauge grid even when hand-authored adjacent fixtures pass.
const historySource = (await readFile('src/host-history.ts', 'utf8')).replace(/^import .*;\n/gm, '');
const aliasSource = await readFile('src/machine-alias.ts', 'utf8');
const usageStoreSource=(await readFile('src/usage-store.ts','utf8')).replace(/^import .*;\n/gm,'');
const compiled = stripTypeScriptTypes(usageStoreSource + '\n' + aliasSource + '\n' + historySource, {mode:'transform'});
const { HostHistoryService } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
const db = new DatabaseSync(':memory:');
const sql = {exec(query, ...args) {
  const stmt = db.prepare(query);
  const rows = stmt.columns().length ? stmt.all(...args) : (stmt.run(...args), []);
  return Object.assign(rows, {toArray:()=>rows});
}};
sql.exec('CREATE TABLE machines(id TEXT,name TEXT,hostname TEXT,drained INTEGER,last_checkin_at TEXT)');
let receiptClock=end;
const nativeHistory = new HostHistoryService(sql, {}, () => receiptClock);
nativeHistory.init();
for (const [ago,value] of [[3,100],[2,200],[1,0]]) {
  nativeHistory.record('tokens', 'codex / shared-codex-old / agent-hub', 'hetzner', end-ago*3600000,
    {input_tokens:value,output_tokens:10}, 'native_token_usage');
}
nativeHistory.record('tokens', 'codex / shared-codex-old / agent-hub', 'hetzner', end-2*3600000+10*60000,
  {input_tokens:250,output_tokens:20}, 'native_token_usage');
// A later renamed-account receipt starts at revision zero; it must still
// replace the old alias's synthetic H+10min correction.
receiptClock++;
nativeHistory.record('tokens','codex / shared-codex / agent-hub','hetzner',end-2*3600000,
  {input_tokens:300,output_tokens:30},'native_token_usage');
// Exercise the real source-table ingestion used by sample(), not record().
// The alias changes during a continuous 10-minute stream, all off-grid.
sql.exec('CREATE TABLE server_quota_snapshots(account TEXT PRIMARY KEY,captured_at TEXT,quota_json TEXT)');
sql.exec('CREATE TABLE fleet_subscriptions(account TEXT,observed_at TEXT,quota_json TEXT)');
for(let minute=2;minute<24*60;minute+=10) {
  if(minute>=6*60 && minute<7*60) continue; // Genuine outage stays a gap.
  const at=start+minute*60000;
  const account=minute<12*60?'shared-codex-old':'shared-codex';
  sql.exec('DELETE FROM server_quota_snapshots');
  sql.exec('INSERT INTO server_quota_snapshots VALUES(?,?,?)',account,new Date(at).toISOString(),JSON.stringify({windows:{
    '5h':{present:true,percent_remaining:90-minute/60},
    '7d':{present:true,percent_remaining:75-minute/120,reset_at:new Date(minute<=12*60?start+12*3600000:end+5*3600000).toISOString()}
  }}));
  nativeHistory.quotaWindows(at);
}
const apiByRange = Object.fromEntries(['24h','7d','30d'].map(range => [range,nativeHistory.query(range)]));
for (const response of Object.values(apiByRange)) {
  const input=response.series.find(row=>row.metric==='input_tokens');
  assert.equal(input.step_ms,3600000);
  assert.deepEqual(input.points.filter(p=>p.value!==null).map(p=>p.value),[100,250,0]);
}
db.close();
const sources = [
  { engine: 'codex', supported: true, detail: 'codex rollout token_usage_record (shared-codex and orin homes)' },
  { engine: 'gemini', supported: false, detail: 'no native per-session token log on the host; quota percentages only' },
];
await mkdir('.local/usage-history', { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || (existsSync(chromium.executablePath()) ? chromium.executablePath() : '/home/alexey/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell'), args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--num-raster-threads=1'] });
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
        const response=apiByRange[range];
        return route.fulfill({json:response});
      }
      if (u.pathname === '/api/quota') return route.fulfill({ json: { canonical: {accounts: {'urgent-muse':'shared-go','shared-codex-old':'shared-codex'}}, pools: [{account:'shared-codex',provider:'codex',harness:['codex'],windows:{'5h':{present:true,percent_remaining:80,reset_at:'2026-10-28T12:00:00Z'},'7d':{present:true,percent_remaining:25,reset_at:'2026-11-01T12:00:00Z'}},native_balance:{remaining:42,unit:'credits'}}] } });
      if (u.pathname === '/api/quota/resets') return route.fulfill({json:{grants:[{id:'reset-1',account:'shared-codex',label:'Banked weekly reset',active:1,expires_at:Date.now()+24*3600000}],observations:[]}});
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
    const current=await page.locator('#usage-current').innerText();
    assert.match(current,/20%/);assert.match(current,/75%/);assert.match(current,/42 credits/);
    assert.match(current,/28 Oct, 13:00 CET/);assert.match(current,/Expiring soon/);
    assert.match(current,/Alexey approval required/);
    assert.equal(await page.locator('#usage-current button').count(),0,'no reset application control');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page fits viewport; table scrolls inside its container');
    // Both known windows share one account chart; null monthly leaves no trace.
    assert.match(await page.locator('#usage-quota-history').innerText(), /codex\/codex shared-codex/);
    assert.equal(await page.locator('#usage-quota-history svg').count(), 1);
    assert.equal(await page.locator('#usage-quota-history .metrics-line').count(), 2);
    const quotaPath=await page.locator('#usage-quota-history .metrics-line').first().getAttribute('d');
    assert.equal((quotaPath.match(/L/g)||[]).length,90,'alias merge connects all 92 known buckets except genuine outage');
    const quotaTimes=apiByRange['24h'].series.filter(s=>s.kind==='quota_window'&&s.metric==='5h').flatMap(s=>s.points.filter(p=>p.value!==null));
    assert.ok(quotaTimes.every(p=>p.at===Date.parse(p.observed_at)&&p.bucket_at<=p.at&&p.at<p.bucket_at+15*60000),'actual timestamps retained separately from bucket identity');
    assert.equal((quotaPath.match(/M/g)||[]).length,2,'only the genuine outage breaks the continuous alias line');
    // Reset marker drawn where the reset time changed.
    assert.equal(await page.locator('#usage-quota-history line.metrics-reset').count(), 1);
    assert.match(await page.locator('#usage-quota-history line.metrics-reset title').textContent(), new RegExp(new Date(start+12*3600000).toISOString()));
    assert.match(await page.locator('#usage-quota-history').innerText(), /reset/);
    // Measured zero is plotted; gaps break the line.
    const d = await page.locator('#usage-token-history .metrics-line').first().getAttribute('d');
    assert.ok(d.includes('L'), 'real token LINE chart from API grid');
    assert.equal(await page.locator('#usage-token-history [data-value="250"]').count(),0,'superseded correction absent');
    assert.ok(await page.locator('#usage-token-history [data-value="300"]').count()>0,'corrected hour rendered');
    assert.ok(await page.locator('#usage-token-history [data-value="0"]').count() > 0, 'measured zero tokens shown');
    assert.equal(await page.locator('#usage-token-history svg').count(), 1);
    assert.match(await page.locator('#usage-token-history').innerText(), /codex\/codex shared-codex \/ agent-hub/);
    assert.doesNotMatch(await page.locator('#usage-token-history').innerText(), /shared-codex-old/);
    // Per-engine provenance instead of a blanket Unknown.
    assert.match(await page.locator('#usage-token-sources').innerText(), /codex: native history/);
    assert.match(await page.locator('#usage-token-sources').innerText(), /gemini: not collected/);
    // Range selector offers 24h/7d/30d and refetches.
    for (const value of ['7d', '30d', '24h']) {
      await page.getByLabel('Usage history range', { exact: true }).selectOption(value);
      await page.waitForFunction(v => document.querySelector('#usage-history [data-usage-note]').textContent.startsWith(v + ' ·'), value);
      assert.equal(range, value);
      assert.ok((await page.locator('#usage-token-history .metrics-line').first().getAttribute('d')).includes('L'), `${value} hourly API points connect`);
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log('PASS usage history: per-window quota lines, reset markers, token lines, known zero, per-engine sources, 24h/7d/30d.');
} finally { await browser.close(); }
