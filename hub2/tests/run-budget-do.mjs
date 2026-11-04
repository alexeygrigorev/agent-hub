// Native SQLite semantic tests plus a real local workerd persistence/restart gate.
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { resolve } from 'node:path';

registerHooks({ resolve(specifier, context, next) {
  if (specifier.startsWith('.') && context.parentURL && !specifier.endsWith('.ts')) {
    const candidate = new URL(specifier + '.ts', context.parentURL);
    if (existsSync(fileURLToPath(candidate))) return next(candidate.href, context);
  }
  return next(specifier, context);
} });
await import('./budget-do.test.ts');

const root = resolve(import.meta.dirname, '../..');
const hub2 = resolve(root, 'hub2');
const fixture = `
import { BudgetDO } from './src/budget-do.ts';
export { BudgetDO };
export default { async fetch(request, env) {
  const url = new URL(request.url);
  const instance = url.searchParams.get('instance') || 'global';
  url.pathname = url.pathname.replace(/^\\/_test/, '') || '/';
  url.searchParams.delete('instance');
  const stub = env.BUDGETS.get(env.BUDGETS.idFromName(instance));
  return stub.fetch(new Request('https://budget.internal' + url.pathname + url.search, request));
} };
`;
const bundle = await build({
  stdin: { contents: fixture, resolveDir: hub2, sourcefile: 'budget-entry.ts', loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'],
});
await mkdir(resolve(root, '.local/tmp'), { recursive: true });
const persist = await mkdtemp(resolve(root, '.local/tmp/hub2-budget-workerd-'));
const options = convertV4MiniflareOptions({
  resourcePersistencePath: persist, host: '127.0.0.1', port: 0,
  log: new Log(LogLevel.ERROR),
  workers: [{
    name: 'hub2-budget-ledger-fixture', script: bundle.outputFiles[0].text,
    modules: true, compatibilityDate: '2024-04-05',
    durableObjects: { BUDGETS: { className: 'BudgetDO', useSQLite: true } },
  }],
});

let mf = new Miniflare(options), base = '';
const admin = { id: 'operator:workerd-budget-fixture', kind: 'operator', scopes: ['operator:admin', 'budget:read'] };
async function api(instance, path, method = 'GET', body) {
  const url = new URL(base + '/_test' + path);
  url.searchParams.set('instance', instance);
  const response = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(admin) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let result;
  try { result = await response.json(); } catch { result = { error: 'invalid_json' }; }
  return { status: response.status, result };
}
const metrics = {
  worker_requests: 100_001, worker_cpu_ms: 0, do_requests: 0, do_ws_messages: 0,
  do_gb_ms: 0, background_invocations: 0, sqlite_rows_read: 0,
  sqlite_rows_written: 0, sqlite_byte_days: 0, kv_reads: 0, kv_writes: 0,
  kv_deletes: 0, kv_lists: 0, kv_byte_days: 0,
};
const coverage = Object.fromEntries(Object.keys(metrics).map(k => [k, 'exact']));
try {
  base = (await mf.ready).origin;
  const reserved = await api('persistent', '/_internal/reserve', 'POST', { id: 'workerd-restart-crossing', kind: 'test', estimated_units: metrics, coverage });
  assert.equal(reserved.status, 200);
  const settled = await api('persistent', '/_internal/settle', 'POST', { id: 'workerd-restart-crossing', actual_units: metrics, coverage });
  assert.equal(settled.status, 200);
  const first = await api('persistent', '/api/budget');
  assert.equal(first.status, 200);
  assert.equal(first.result.alerts.filter(v => v.kind === 'threshold').length, 1);

  await mf.dispose();
  mf = new Miniflare(options);
  base = (await mf.ready).origin;
  const afterRestart = await api('persistent', '/api/budget');
  assert.equal(afterRestart.result.alerts.filter(v => v.kind === 'threshold').length, 1);
  assert.equal(afterRestart.result.settled_usd, '0.030000300000');
  const replay = await api('persistent', '/_internal/settle', 'POST', { id: 'workerd-restart-crossing', actual_units: metrics, coverage });
  assert.equal(replay.status, 200);
  assert.equal(replay.result.replay, true);
  const outbox = await api('persistent', '/_internal/alerts');
  assert.equal(outbox.result.items.filter(v => v.kind === 'threshold').length, 1);
  console.log(JSON.stringify({ passed: true, gate: 'workerd SQLite object restart', daily_alerts_after_restart: afterRestart.result.alerts, settled_usd: afterRestart.result.settled_usd, note: 'Synthetic cost units; no Cloudflare billing/API or production data accessed.' }));
} finally {
  await mf.dispose();
  await rm(persist, { recursive: true, force: true });
}
