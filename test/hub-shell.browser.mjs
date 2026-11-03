import assert from 'node:assert/strict';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { launchBrowser } from './playwright.mjs';

const root = resolve(import.meta.dirname, '..');
const evidence = process.env.HUB_UI_EVIDENCE_DIR || '/home/alexey/tmp/agent-reports/hub-ui-chunk1';
await mkdir(evidence, { recursive: true, mode: 0o700 });
let shellSource = await readFile(resolve(root, 'public/hub-shell.js'), 'utf8');
shellSource = shellSource.replace("import { mountHubSearch } from '/hub-search.js';", 'const mountHubSearch = window.mountHubSearch;').replace(/^export /gm, '');
const searchSource = (await readFile(resolve(root, 'public/hub-search.js'), 'utf8')).replace(/^export /gm, '');
const styles = await readFile(resolve(root, 'public/hub-shell.css'), 'utf8');
const pages = ['/index.html','/sessions.html','/intake.html','/fleet.html','/quota.html','/machines.html','/settings.html','/inbox.html','/conversations.html','/metrics.html','/schedules.html','/prompts.html','/webhooks.html','/engines.html','/banked-resets.html','/asks.html'];
let browser;
try {
  browser = await launchBrowser({ args: ['--single-process','--in-process-gpu','--disable-gpu','--disable-dev-shm-usage','--no-sandbox','--js-flags=--thread-pool-size=1','--num-raster-threads=1','--disable-threaded-animation','--disable-threaded-scrolling'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route('https://shell.test/**', route => route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><head><title>Hub</title></head><body><header class="dk-topbar">Old header</header><aside id="dk-sidebar" class="dk-sidebar">Old navigation</aside><nav class="hub-mobile-nav">Legacy mobile links</nav><a data-hub-new-session href="/fleet.html#launch">Legacy launch</a><main class="dk-main" id="main-content"><h1>Page body remains owned by page worker</h1><p>Content</p></main></body></html>' }));
  await page.route('**/api/ui-search**', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ query: '', scope: 'active', results: [], next_cursor: null, partial: false, scanned: 0 }) }));
  for (const path of pages) {
    await page.goto(`https://shell.test${path}`);
    await page.evaluate(({ shell, search, css }) => {
      const style = document.createElement('style'); style.textContent = css; document.head.append(style);
      window.mountHubSearch = new Function(search + '\nreturn mountHubSearch;')();
      window.mountHubShell = new Function(shell + '\nreturn mountHubShell;')();
      window.mountHubShell();
    }, { shell: shellSource, search: searchSource, css: styles });
    await page.getByRole('button', { name: 'New session' }).waitFor();
    await page.getByRole('button', { name: 'Search sessions, tasks, and machines' }).waitFor();
    assert.equal(await page.locator('.hub-shell__sidebar .hub-shell__nav-link').count(), 8, `${path} has eight shared destinations`);
    for (const [label, href] of [['Sessions','/sessions.html'],['Tasks','/intake.html'],['Quota','/quota.html'],['Machines','/machines.html'],['Messages','/inbox.html'],['Metrics','/metrics.html'],['Automation','/schedules.html'],['Settings','/settings.html']]) {
      assert.equal(await page.locator(`.hub-shell__sidebar a:has-text("${label}")`).getAttribute('href'), href, `${label} has the shared destination`);
    }
    assert.equal(await page.locator('main#main-content h1').textContent(), 'Page body remains owned by page worker');
    assert.equal(await page.locator('body > header.dk-topbar').evaluate(el => getComputedStyle(el).display), 'none', 'old header is normalized');
    assert.equal(await page.locator('.hub-mobile-nav').evaluate(el => getComputedStyle(el).display), 'none', 'legacy mobile destinations are replaced without duplication');
    assert.equal(await page.locator('[data-hub-new-session]').evaluate(el => getComputedStyle(el).display), 'none', 'legacy launch shortcut is replaced without duplication');
    if (path === '/sessions.html') {
      await page.evaluate(() => { window.newSessionRequested = false; document.body.addEventListener('hub:new-session', event => { event.preventDefault(); window.newSessionRequested = true; }); });
      await page.getByRole('button', { name: 'New session' }).click();
      assert.equal(await page.evaluate(() => window.newSessionRequested), true, 'New session reaches the page-owned launch dialog hook');
      await page.screenshot({ path: resolve(evidence, 'after-desktop-1440x900.png'), fullPage: false });
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('https://shell.test/sessions.html');
  await page.evaluate(({ shell, search, css }) => {
    const style = document.createElement('style'); style.textContent = css; document.head.append(style);
    window.mountHubSearch = new Function(search + '\nreturn mountHubSearch;')();
    window.mountHubShell = new Function(shell + '\nreturn mountHubShell;')();
    window.mountHubShell();
  }, { shell: shellSource, search: searchSource, css: styles });
  await page.getByRole('button', { name: 'New session' }).waitFor();
  assert.equal(await page.locator('.hub-shell__bottom').isVisible(), true);
  assert.equal(await page.locator('.hub-shell[data-page="/sessions.html"] .hub-shell__attention').isVisible(), true, 'Sessions keeps its attention chip visible on mobile');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  assert.equal(overflow, false, 'shared shell fits 390px viewport without horizontal overflow');
  await page.getByRole('button', { name: 'Search sessions, tasks, and machines' }).click();
  assert.equal(await page.getByRole('dialog', { name: 'Search the Hub' }).isVisible(), true, 'mobile search affordance opens the search dialog');
  await page.keyboard.press('Escape');
  await page.screenshot({ path: resolve(evidence, 'after-mobile-390x844.png'), fullPage: false });
  console.log(`PASS shared shell across ${pages.length} core and administrative pages at desktop; 390px mobile search, launch, attention and bottom navigation`);
} finally {
  if (browser) await browser.close();
}
