import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { launchBrowser } from './playwright.mjs';

const root = resolve(import.meta.dirname, '..');
const source = (await readFile(resolve(root, 'public/hub-search.js'), 'utf8')).replace(/^export /gm, '');
let browser;
let failedOnce = false;
try {
  browser = await launchBrowser({ args: ['--single-process','--in-process-gpu','--disable-gpu','--disable-dev-shm-usage','--no-sandbox','--js-flags=--thread-pool-size=1','--num-raster-threads=1','--disable-threaded-animation','--disable-threaded-scrolling'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.route('https://ui.test/**', async route => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.pathname === '/api/ui-search') {
      const q = requestUrl.searchParams.get('q');
      if (q === 'fail' && !failedOnce) { failedOnce = true; return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'search_unavailable' }) }); }
      if (q === 'older') await new Promise(resolve => setTimeout(resolve, 450));
      const result = q === 'new'
        ? [{ id: 'session-1', kind: 'sessions', title: 'Friendly session', status: 'running', context: 'codex · host-a', href: '/sessions.html?session=session-1' }]
        : [];
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ query: q, scope: requestUrl.searchParams.get('scope'), results: result, next_cursor: null, partial: false, scanned: 3 }) });
    }
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><body><button id="search">Search</button><main id="main-content"></main></body></html>' });
  });
  await page.goto('https://ui.test/sessions.html');
  await page.addStyleTag({ content: '.hub-search[hidden]{display:none}.hub-search{position:fixed;inset:0;background:#0008}.hub-search__panel{background:white;padding:16px}.hub-search__result[aria-selected=true]{outline:2px solid green}' });
  await page.evaluate(sourceText => {
    const compiled = sourceText + '\nwindow.mountHubSearch = mountHubSearch;';
    const script = document.createElement('script'); script.textContent = compiled; document.body.append(script);
    const trigger = document.getElementById('search');
    window.mountHubSearch(trigger);
  }, source);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Search the Hub' });
  const input = dialog.getByRole('searchbox');
  assert.equal(await input.evaluate(el => el === document.activeElement), true, 'opening search focuses its field');
  await input.fill('older');
  await page.waitForResponse(response => new URL(response.url()).searchParams.get('q') === 'older');
  await input.fill('new');
  await page.getByRole('option', { name: /Friendly session/ }).waitFor();
  await page.waitForTimeout(550);
  assert.equal(await page.getByRole('option', { name: /Friendly session/ }).count(), 1, 'a late stale result cannot replace the current query');
  assert.equal(await dialog.getByText(/complete/).count(), 1, 'reports complete results separately from partial scans');
  await page.keyboard.press('ArrowDown');
  assert.equal(await dialog.locator('.hub-search__result[aria-selected="true"]').count(), 1, 'arrow keys select a result');
  await page.keyboard.press('Escape');
  assert.equal(await dialog.isVisible(), false, 'Escape closes search');
  assert.equal(await page.getByRole('button', { name: 'Search', exact: true }).evaluate(el => el === document.activeElement), true, 'focus returns to the opener');

  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await dialog.getByRole('searchbox').fill('missing');
  await dialog.getByText(/No matches/).waitFor();
  await dialog.getByRole('searchbox').fill('fail');
  await dialog.getByText(/temporarily unavailable/).waitFor();
  await dialog.getByRole('button', { name: 'Retry search' }).click();
  await dialog.getByText(/No matches/).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await dialog.getByRole('searchbox').fill('new');
  await page.getByRole('option', { name: /Friendly session/ }).waitFor();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await page.waitForURL(/session=session-1/);
  const destination = new URL(page.url());
  assert.equal(destination.searchParams.get('q'), 'new', 'result navigation preserves the query');
  assert.equal(destination.searchParams.get('hubSearch'), null, 'the entity destination is not obscured by the search dialog');
  await page.goBack();
  await page.waitForURL(/hubSearch=1/);
  assert.equal(new URL(page.url()).searchParams.get('q'), 'new', 'Back restores the query on the search page');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('https://ui.test/sessions.html');
  await page.evaluate(sourceText => {
    const compiled = sourceText + '\nwindow.mountHubSearch = mountHubSearch;';
    const script = document.createElement('script'); script.textContent = compiled; document.body.append(script);
    window.mountHubSearch(document.getElementById('search'));
  }, source);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  assert.equal(await dialog.isVisible(), true, 'search remains available in the mobile viewport');
  await page.keyboard.press('Escape');
  console.log('PASS global search keyboard, stale-query cancellation, focus return, and 390px mobile dialog');
  await context.close();
} finally {
  if (browser) await browser.close();
}
