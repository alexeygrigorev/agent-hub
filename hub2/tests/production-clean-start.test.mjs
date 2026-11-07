// Clean-start invariants on the Node runtime: bootstrap gate scope, fresh DB
// initialization, and durable persistence across a service restart.
import assert from 'node:assert/strict';
import { mkdtemp, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createFixture, restartFixture, makeTempDir } from './node-fixture.mjs';

const root = resolve(import.meta.dirname, '../..');
const { default: entry, ControlDO, ProjectDO, MachineDO, AccountDO, MailboxDO } = await import('../production-worker.ts');
const dataDir = await makeTempDir('clean-start-');
const spec = {
  root,
  entry,
  classes: { CONTROL: ControlDO, PROJECTS: ProjectDO, MACHINES: MachineDO, ACCOUNTS: AccountDO, MAILBOXES: MailboxDO },
  env: { HUB2_RELEASE_MODE: 'bootstrap', HUB2_BOOTSTRAP_TOKEN: 'clean-start-token-0000000000000000000', HUB2_CREDENTIAL_KEY: 'ab'.repeat(32) },
  dataDir,
};
const step = (name) => console.error('step:' + name);
let fixture = await createFixture(spec);
const get = async (path, init) => fixture.fetch(path, init);

step('health');
const health = await get('/health');
assert.equal(health.status, 200);
assert.deepEqual(await health.json(), { status: 'bootstrap', dispatch_enabled: false });

step('gated');
assert.equal((await get('/api/intakes')).status, 503, 'routes outside the bootstrap allowlist must be refused');

step('pairing');
const device = await get('/api/device/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
assert.equal(device.status, 200);
assert.equal(typeof (await device.json()).device_code, 'string', 'pairing endpoints stay available in bootstrap');

step('files');
const databases = (await readdir(dataDir)).filter((n) => n.endsWith('.sqlite'));
assert.ok(databases.some((n) => n.startsWith('CONTROL-')), 'control DO must materialize its own SQLite file: ' + databases.join(','));

step('restart');
fixture = await restartFixture(fixture, spec);

step('health2');
assert.deepEqual(await (await get('/health')).json(), { status: 'bootstrap', dispatch_enabled: false });
await fixture.dispose();
console.log(JSON.stringify({ passed: true, databases: databases.length }));
