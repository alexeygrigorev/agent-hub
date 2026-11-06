import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {build} from 'esbuild';

const root = resolve(import.meta.dirname, '../..');
const configText = await readFile(resolve(root, 'hub2/wrangler.production.jsonc'), 'utf8');
const config = JSON.parse(configText.replace(/^\s*\/\/.*$/gm, ''));
const worker = await readFile(resolve(root, 'hub2/production-worker.ts'), 'utf8');
const freshClasses = ['ControlDO', 'ProjectDO', 'MachineDO', 'AccountDO', 'MailboxDO', 'BudgetDO'];

assert.equal(config.name, 'agent-hub', 'cleanup cannot target an unrelated Worker');
assert.equal(config.main, 'production-worker.ts');
assert.deepEqual(Object.keys(config.exports).sort(), ['Hub', ...freshClasses].sort(), 'no unrelated namespace lifecycle changes');
for (const binding of config.durable_objects.bindings) {
  assert.equal(Object.hasOwn(binding, 'script_name'), false, 'no external Worker namespace binding');
  assert.equal(Object.hasOwn(binding, 'namespace_id'), false, 'no existing unrelated namespace identity');
}
assert.equal(Object.keys(config.vars).some(key => /HUB1_|HUB2_IMPORT|DEV_OPERATOR_TOKEN/.test(key)), false);
assert.equal(config.vars.HUB2_RELEASE_MODE, 'maintenance', 'first deployment keeps dispatch disabled');
assert.equal(config.workers_dev, true, 'production deployment keeps the approved Worker route enabled');
assert.deepEqual(config.vars, {
  HUB2_RELEASE_MODE: 'maintenance',
  AUTH_CLIENT_ID: config.vars.AUTH_CLIENT_ID,
  AUTH_ISSUER: config.vars.AUTH_ISSUER,
  AUTH_BASE_URL: config.vars.AUTH_BASE_URL,
  AUTH_CALLBACK_URL: config.vars.AUTH_CALLBACK_URL,
  AUTH_JWKS_URL: config.vars.AUTH_JWKS_URL,
  HUB_ADMINS: config.vars.HUB_ADMINS,
}, 'the clean-start config keeps only public auth/operator metadata; runtime secrets are installed separately');
assert.deepEqual(config.triggers.crons, ['*/15 * * * *'], 'the alert cron is installed but the Worker gate makes it a no-op until active');
assert.equal(Object.hasOwn(config, 'migrations'), false, 'the clean-start export manifest replaces old migrations');
assert.deepEqual(
  Object.keys(config.durable_objects.bindings).length === 0 ? [] : config.durable_objects.bindings.map(binding => binding.class_name).sort(),
  [...freshClasses].sort(),
  'production binds only fresh Hub2 namespaces',
);
assert.deepEqual(config.exports.Hub, {type: 'durable-object', state: 'deleted'}, 'the old Hub namespace is explicitly deleted');
for (const name of freshClasses) {
  assert.deepEqual(config.exports[name], {type: 'durable-object', storage: 'sqlite'}, `${name} starts as a fresh SQLite namespace`);
  assert(config.durable_objects.bindings.some(binding => binding.class_name === name), `${name} has a production binding`);
}
assert.equal(config.durable_objects.bindings.some(binding => binding.class_name === 'Hub'), false);
assert.doesNotMatch(configText, /HUB1_EXPORT|HUB2_IMPORT|HUB2_IMPORT_MANIFEST/);
assert.doesNotMatch(worker, /\bclass\s+Hub\b|HUB1_EXPORT|HUB2_IMPORT|\/api\/backup|\/_maintenance/);
for (const name of freshClasses) assert.match(worker, new RegExp(`export \\{ ${name} \\} from`));
assert.match(worker, /HUB2_RELEASE_MODE === 'active'/, 'scheduled work only runs after explicit activation');
assert.match(worker, /HUB2_RELEASE_MODE === 'bootstrap'/, 'bootstrap is an explicit intermediate state');
assert.match(worker, /bootstrapRequestAllowed\(request\)/, 'bootstrap mode uses an explicit method/path allowlist');

// Execute the actual composition. Maintenance must not touch auth, SQL, native or provider bindings.
const bundle = await build({
  entryPoints: [resolve(root, 'hub2/production-worker.ts')], bundle: true, write: false,
  format: 'esm', platform: 'node', external: ['cloudflare:workers', 'node:async_hooks'],
});
const production = (await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'))).default;
let accessed = 0;
const environment = mode => new Proxy({HUB2_RELEASE_MODE: mode}, {
  get(target, key) {
    if (key === 'HUB2_RELEASE_MODE') return target[key];
    accessed++;
    throw new Error('unexpected maintenance binding access');
  },
});
for (const mode of [undefined, 'maintenance', 'ACTIVE']) {
  const env = environment(mode);
  const health = await production.fetch(new Request('https://synthetic.invalid/health'), env);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), {status: 'maintenance', dispatch_enabled: false});
  for (const [method, path] of [['GET', '/api/repos'], ['POST', '/api/intakes'], ['POST', '/api/maintenance/task']]) {
    const response = await production.fetch(new Request('https://synthetic.invalid' + path, {method}), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {error: 'release_maintenance', dispatch_enabled: false});
  }
  await production.scheduled({}, env);
}
assert.equal(accessed, 0, 'maintenance must not instantiate any namespace or access provider credentials');

const receiptResponse = (request, payload, status = 200) => {
  const operation = request.headers.get('x-hub2-sql-operation');
  const headers = new Headers({'Content-Type': 'application/json'});
  if (operation) headers.set('x-hub2-sql-receipt', JSON.stringify({v: 1, valid: true, reads: 0, writes: 0, operation_id: operation}));
  return new Response(JSON.stringify(payload), {status, headers});
};
const metricNames = ['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
const bootstrapUnits = Object.fromEntries(metricNames.map(name => [name, 100000]));
const bootstrapEnvelope = JSON.stringify({
  version: 1,
  normal: {units: bootstrapUnits, max_do_calls: 16, max_wall_ms: 10000, provenance: 'composition test only'},
  safety: {units: bootstrapUnits, max_do_calls: 16, max_wall_ms: 10000, provenance: 'composition test only'},
});
const calls = [];
const namespace = fetch => ({
  idFromName: name => name,
  get: () => ({fetch}),
});
const control = namespace(async input => {
    const request = input instanceof Request ? input : new Request(input);
    const path = new URL(request.url).pathname;
    calls.push(path);
    if (path === '/_verify') {
      const body = await request.json();
      const actor = body.token === 'm'.repeat(40)
        ? {id: 'machine:host-bootstrap', kind: 'machine', resource_id: 'host-bootstrap', scopes: ['machine:heartbeat'], generation: 'fixture'}
        : {id: 'human:bootstrap-test', kind: 'operator', scopes: ['operator:admin','repos:read','budget:read'], generation: 'fixture'};
      return receiptResponse(request, {actor});
    }
    if (path === '/api/enrollments/complete') return receiptResponse(request, {machine_id: 'host-bootstrap'}, 201);
    if (path === '/api/enrollments/pair') return receiptResponse(request, {pairing_id: 'pairing', pairing_code: 'c'.repeat(32)}, 201);
    if (path === '/api/repos') return receiptResponse(request, {items: [], next: null});
    if (path === '/api/repos/owner%2Frepo') return receiptResponse(request, {repo: {canonical_name: 'owner/repo', status: 'active'}});
    if (path === '/api/model-policy') return receiptResponse(request, {version: 1, candidates: []});
    return receiptResponse(request, {ok: true});
  });
const budget = namespace(async input => {
    const request = input instanceof Request ? input : new Request(input);
    const path = new URL(request.url).pathname;
    if (path === '/_internal/reserve') return receiptResponse(request, {allowed: true, retry_at: Date.now() + 86400000});
    if (path === '/_internal/settle') return receiptResponse(request, {settled: true});
    return receiptResponse(request, {ok: true});
  });
const accounts = namespace(async input => receiptResponse(input instanceof Request ? input : new Request(input), {configured: true}));
const machines = namespace(async input => receiptResponse(input instanceof Request ? input : new Request(input), {heartbeat: true}));
const bootstrapMid = Math.floor(bootstrapEnvelope.length / 2);
const bootstrapEnv = {HUB2_RELEASE_MODE: 'bootstrap', HUB2_BUDGET_ENVELOPES: bootstrapEnvelope.slice(0, bootstrapMid), HUB2_BUDGET_ENVELOPES_PART2: bootstrapEnvelope.slice(bootstrapMid), CONTROL: control, BUDGET: budget, ACCOUNTS: accounts, MACHINES: machines};
const health = await production.fetch(new Request('https://synthetic.invalid/health'), bootstrapEnv);
assert.equal(health.status, 200);
assert.deepEqual(await health.json(), {status: 'bootstrap', dispatch_enabled: false});
for (const [method, path] of [
  ['POST', '/api/intakes'], ['POST', '/api/schedules'], ['POST', '/api/claims'], ['POST', '/api/messages'],
  ['GET', '/api/offers'], ['GET', '/api/connect'], ['POST', '/api/sessions/synthetic/start'],
  ['POST', '/api/account/reserve'], ['PUT', '/api/accounts/zai-central/api/account/quota'],
]) {
  const denied = await production.fetch(new Request('https://synthetic.invalid' + path, {method}), bootstrapEnv);
  assert.equal(denied.status, 503, `bootstrap blocks dispatch-capable route ${method} ${path}`);
  assert.deepEqual(await denied.json(), {error: 'release_bootstrap', dispatch_enabled: false});
}
const repoList = await production.fetch(new Request('https://synthetic.invalid/api/repos', {headers: {Authorization: 'Bearer ' + 'b'.repeat(40)}}), bootstrapEnv);
assert.equal(repoList.status, 200, 'bootstrap exposes the authenticated canonical project registry read');
assert.deepEqual(await repoList.json(), {items: [], next: null});
const repoWrite = await production.fetch(new Request('https://synthetic.invalid/api/repos/owner%2Frepo', {
  method: 'PATCH', headers: {Authorization: 'Bearer ' + 'b'.repeat(40), 'Content-Type': 'application/json'},
  body: JSON.stringify({canonical_url: 'https://github.com/owner/repo', purpose: 'bootstrap registry', status: 'active'}),
}), bootstrapEnv);
assert.equal(repoWrite.status, 200, 'bootstrap can register the canonical project under operator auth: ' + await repoWrite.clone().text());
const policy = await production.fetch(new Request('https://synthetic.invalid/api/model-policy', {
  method: 'PUT', headers: {Authorization: 'Bearer ' + 'b'.repeat(40), 'Content-Type': 'application/json'}, body: JSON.stringify({expected_version: 0, candidates: []}),
}), bootstrapEnv);
assert.equal(policy.status, 200, 'bootstrap can configure model policy without enabling dispatch');
const account = await production.fetch(new Request('https://synthetic.invalid/api/accounts/zai-central/api/account', {
  method: 'PATCH', headers: {Authorization: 'Bearer ' + 'b'.repeat(40), 'Content-Type': 'application/json'},
  body: JSON.stringify({provider: 'zai', enabled: true, capacity: 1, min_remaining: 10}),
}), bootstrapEnv);
assert.equal(account.status, 200, 'bootstrap can configure a provider account behind the AccountDO authorization: ' + await account.clone().text());
const credentialSetup = await production.fetch(new Request('https://synthetic.invalid/api/accounts/zai-central/api/account/credentials', {
  method: 'PUT', headers: {Authorization: 'Bearer ' + 'b'.repeat(40), 'Content-Type': 'application/json'},
  body: JSON.stringify({expected_generation: 0, files: {'.config/goz/config.json': JSON.stringify({access_token: 'fixture'})}}),
}), bootstrapEnv);
assert.equal(credentialSetup.status, 200, 'bootstrap can install an approved account credential bundle through AccountDO');
const heartbeat = await production.fetch(new Request('https://synthetic.invalid/api/heartbeat', {
  method: 'POST', headers: {Authorization: 'Bearer ' + 'm'.repeat(40), 'Content-Type': 'application/json'},
  body: JSON.stringify({os: 'linux', arch: 'x64', labels: [], engines: ['codex'], capabilities: ['headless'], resources: {mem_available_mb: 12000}}),
}), bootstrapEnv);
assert.equal(heartbeat.status, 200, 'bootstrap can enroll a host resource snapshot without opening its offer socket');
const pairing = await production.fetch(new Request('https://synthetic.invalid/api/enrollments/complete', {
  method: 'POST', headers: {'Content-Type': 'application/json', Origin: 'https://synthetic.invalid'}, body: JSON.stringify({code: 'f'.repeat(32)}),
}), bootstrapEnv);
assert.equal(pairing.status, 201, 'bootstrap can complete an explicitly initiated host enrollment');
assert.equal(calls.includes('/api/enrollments/complete'), true);
await production.scheduled({}, bootstrapEnv);
assert.equal(calls.some(path => path.includes('schedule')), false, 'scheduled events remain disabled during bootstrap');

console.log(JSON.stringify({passed: true, gate: 'production clean-start and bootstrap composition', old_hub: 'deleted', fresh_sqlite_classes: freshClasses, maintenance: true, bootstrap: true, bootstrap_dispatch_denials: 9, cron_count: config.triggers.crons.length, actual_maintenance_guard: true, binding_accesses: accessed}));
