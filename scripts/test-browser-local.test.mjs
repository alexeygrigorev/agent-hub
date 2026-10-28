import test from 'node:test';
import assert from 'node:assert/strict';
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

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
