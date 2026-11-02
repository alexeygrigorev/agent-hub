import test from 'node:test';
import assert from 'node:assert/strict';
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { userInfo } from 'node:os';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { PINNED_PLAYWRIGHT_VERSION, ownedPlaywrightPackage, resolvePlaywrightPackage } from '../test/playwright.mjs';

test('discovery runs every suite, aggregates failures and isolates live identity', async () => {
  const parent = resolve(import.meta.dirname, '../.local/tmp');
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(resolve(parent, 'browser-runner-'));
  try {
    await mkdir(resolve(root, 'scripts'));
    await mkdir(resolve(root, 'test'));
    await copyFile(new URL('./test-browser-local.mjs', import.meta.url), resolve(root, 'scripts/test-browser-local.mjs'));
    const record = resolve(root, 'record.jsonl');
    for (const name of ['added.browser.mjs', 'style.checks.mjs', 'machine-pairing.mjs', 'conversation-platform.mjs']) {
      await writeFile(resolve(root, 'test', name), `
        import { appendFileSync } from 'node:fs';
        import assert from 'node:assert/strict';
        assert.equal(process.env.APLEXER_FUTURE_SELECTOR, undefined);
        assert.equal(process.env.HUB_TOKEN, undefined);
        assert.equal(process.env.AGENT_HUB_MANAGED_DIR, undefined);
        assert.equal(process.env.PAIRING_NATIVE_WORKSPACE, '');
        assert.equal(process.env.DTC_AUTH_HOME, '/fixture/auth');
        appendFileSync(process.env.BROWSER_TEST_RECORD, JSON.stringify(${JSON.stringify(name)}) + '\\n');
        process.exitCode = ${name === 'added.browser.mjs' ? 7 : 0};
      `);
    }
    const result = spawnSync(process.execPath, [resolve(root, 'scripts/test-browser-local.mjs')], {
      encoding: 'utf8', timeout: 15_000,
      env: { ...process.env, TMPDIR: resolve(root, '.local/tmp'), BROWSER_TEST_RECORD: record,
        APLEXER_FUTURE_SELECTOR: 'synthetic', HUB_TOKEN: 'synthetic', AGENT_HUB_MANAGED_DIR: '/fixture/managed',
        PAIRING_NATIVE_WORKSPACE: '/fixture/live', DTC_AUTH_HOME: '/fixture/auth' },
    });
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.deepEqual((await readFile(record, 'utf8')).trim().split('\n').map(JSON.parse).sort(),
      ['added.browser.mjs', 'style.checks.mjs', 'machine-pairing.mjs', 'conversation-platform.mjs'].sort());
    assert.match(result.stdout + result.stderr, /FAIL added.browser.mjs: 7/);
    assert.match(result.stdout, /4 standalone suites; 1 failed/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('resolvePlaywrightPackage uses the owned donor despite managed HOME and ignores ad-hoc donor overrides', async () => {
  const parent = resolve(import.meta.dirname, '../.local/tmp');
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(resolve(parent, 'playwright-resolve-'));
  try {
    const repo = resolve(root, 'repo');
    const repoPkg = resolve(repo, 'package.json');
    await mkdir(repo, { recursive: true });
    await writeFile(repoPkg, '{"name":"fixture-repo"}\n');

    const accountHome = resolve(root, 'account-home');
    const managedHome = resolve(root, 'empty-managed-home');
    const ownedPkg = ownedPlaywrightPackage(accountHome);
    await mkdir(resolve(ownedPkg, '..'), { recursive: true });
    await writeFile(ownedPkg, JSON.stringify({ name: 'playwright', version: PINNED_PLAYWRIGHT_VERSION }));

    const oldHome = process.env.HOME;
    const oldPackage = process.env.PLAYWRIGHT_PACKAGE;
    process.env.HOME = managedHome;
    process.env.PLAYWRIGHT_PACKAGE = resolve(root, 'unowned/package.json');
    try {
      assert.equal(ownedPlaywrightPackage(), ownedPlaywrightPackage(userInfo().homedir));
      assert.equal(resolvePlaywrightPackage(repoPkg, accountHome), ownedPkg);
    } finally {
      if (oldHome === undefined) delete process.env.HOME;
      else process.env.HOME = oldHome;
      if (oldPackage === undefined) delete process.env.PLAYWRIGHT_PACKAGE;
      else process.env.PLAYWRIGHT_PACKAGE = oldPackage;
    }

    await mkdir(resolve(repo, 'node_modules/playwright'), { recursive: true });
    const localPackage = resolve(repo, 'node_modules/playwright/package.json');
    await writeFile(localPackage, JSON.stringify({ name: 'playwright', version: PINNED_PLAYWRIGHT_VERSION }));
    assert.equal(resolvePlaywrightPackage(repoPkg, accountHome), localPackage);
  } finally { await rm(root, { recursive: true, force: true }); }
});
