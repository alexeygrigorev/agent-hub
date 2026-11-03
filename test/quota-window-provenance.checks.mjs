import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

test('pool-wide minimum is not assigned to a window without provenance', async () => {
  const source = await readFile(new URL('../public/quota.js', import.meta.url), 'utf8');
  const marker = "  $('quota-refresh').addEventListener('click', refresh);";
  assert.ok(source.includes(marker), 'quota script initialization marker exists');
  const instrumented = source.replace(marker, '  globalThis.quotaTest = { windowsFor, unattributedMinimum }; return;');
  const context = vm.createContext({});
  vm.runInContext(instrumented, context);
  const { windowsFor, unattributedMinimum } = context.quotaTest;
  const pool = {
    provider: 'codex', account: 'example', source: 'machine-fallback', state: 'fresh',
    remaining_percent: 20,
    windows: {
      '5h': { present: true, percent_remaining: 80, reset_at: '2026-11-04T00:00:00Z' },
      '7d': { present: true, percent_remaining: 60, reset_at: '2026-11-10T00:00:00Z' },
    },
  };
  // The pool minimum comes from a second observation whose window details are
  // unavailable to the client; it must not inherit the displayed 7d reset.
  const windows = windowsFor({ hosts: [] }, pool, [pool]);
  assert.deepEqual(Array.from(windows, ([name, win]) => [name, win.percent_remaining, win.reset_at]), [
    ['5h', 80, '2026-11-04T00:00:00Z'],
    ['7d', 60, '2026-11-10T00:00:00Z'],
  ]);
  assert.equal(unattributedMinimum(pool, windows), 20);
  assert.equal(unattributedMinimum({ ...pool, remaining_percent: 60 }, windows), null);
  assert.equal(unattributedMinimum({ ...pool, state: 'stale' }, windows), null);
});
