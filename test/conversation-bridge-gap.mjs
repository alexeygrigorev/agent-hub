// Frozen production schema/API + real local HTTP + actual Python CLI.
// --native additionally uses ONLY the caller's genuine, explicitly approved binding.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { randomBytes, createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, chmod, access, symlink } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const root = resolve(import.meta.dirname, '..');
const scratch = resolve(root, '.local/bridge-gap');
const frozen = 'd9b4cde07e3718d77b2db9308cd323a5c8e003ed';
const native = process.argv.includes('--native');
await mkdir(scratch, { recursive: true, mode: 0o700 });
await chmod(scratch, 0o700);
assert.equal(process.env.TMPDIR, resolve(root, '.local/tmp'));
const dependencies = process.env.HUB_TEST_DEPENDENCIES || resolve(root, '../agent-hub/node_modules');
try { await symlink(dependencies, resolve(scratch, 'node_modules')); } catch (error) { if (error.code !== 'EEXIST') throw error; }
const require = createRequire(resolve(scratch, 'fixture-package.json'));
const { build } = require('esbuild');
const { Miniflare, Log, LogLevel, convertV4MiniflareOptions } = require('miniflare');
const source = resolve(scratch, 'frozen');
const paths = (await exec('git', ['ls-tree', '-r', '--name-only', frozen, 'src', 'config/model-routing.json'], { cwd: root })).stdout.trim().split('\n');
let sourceBytes = 0;
for (const file of paths) {
  const content = (await exec('git', ['show', frozen + ':' + file], { cwd: root, maxBuffer: 2 * 1024 * 1024 })).stdout;
  sourceBytes += Buffer.byteLength(content);
  assert.ok(sourceBytes < 8 * 1024 * 1024);
  await mkdir(dirname(resolve(source, file)), { recursive: true });
  await writeFile(resolve(source, file), content);
}
await mkdir(resolve(source, 'test'), { recursive: true });
await writeFile(resolve(source, 'test/conversation-bridge-gap-worker.ts'), await readFile(resolve(root, 'test/conversation-bridge-gap-worker.ts')));
const cfg = JSON.parse(await readFile(resolve(root, 'test/wrangler.conversation-bridge-gap.jsonc')));
const project = 'gap-local';
let own;
const journal = resolve(root, '.local/conversation-bridge-9b9feba0a4dd961ded6aec98.json');
if (native) {
  own = JSON.parse((await exec('a', ['--json', 'whoami'], { cwd: root })).stdout);
  assert.equal(own.workspace, root);
  assert.equal(own.tag, 'platform-bridge-gap');
  assert.equal(own.id, 'e02dfc7f-3528-4cc7-89d5-2ceda8754263');
  assert.equal(own.engine, 'codex');
  assert.equal(await access(journal).then(() => true, () => false), false,
    'Preserve prior journals/uncertainty; do not rerun with a new fixture to bypass it');
}
const tag = own?.tag || 'gap-http-fixture';
const actor = project + ':' + tag;
const nonce = randomBytes(8).toString('hex');
const key = randomBytes(32).toString('hex');
const credentials = [
  { id: 'gap-agent', token: randomBytes(32).toString('hex'), scopes: ['bus:read', 'bus:send'] },
  { id: 'gap-revoked', token: randomBytes(32).toString('hex'), scopes: ['bus:read', 'bus:send'], revoked: true },
  { id: 'gap-scopeless', token: randomBytes(32).toString('hex'), scopes: [] },
  { id: 'gap-host', token: randomBytes(32).toString('hex'), scopes: ['bus:read', 'bus:send'], kind: 'host' },
  { id: 'gap-foreign', token: randomBytes(32).toString('hex'), scopes: ['bus:read', 'bus:send'], actor: 'gap-local:foreign' },
];
const bundle = await build({ entryPoints: [resolve(source, 'test', cfg.main)], bundle: true, write: false,
  format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const mf = new Miniflare(convertV4MiniflareOptions({ host: '127.0.0.1', port: 0, log: new Log(LogLevel.ERROR),
  workers: [{ name: cfg.name, script: bundle.outputFiles[0].text, modules: true,
    compatibilityDate: cfg.compatibility_date, compatibilityFlags: cfg.compatibility_flags,
    durableObjects: { HUB: { className: 'Hub', useSQLite: true } },
    bindings: { HUB_ADMINS: 'gap-fixture@fake.local', SESSION_SECRET: randomBytes(32).toString('hex'), GAP_FIXTURE_KEY: key },
  }],
}));
const checks = [];
const check = name => { checks.push(name); console.log('PASS ' + name); };
const deadline = setTimeout(() => { console.error('Fixture deadline'); process.exit(2); }, 180000);
let evidence;
try {
  const base = (await mf.ready).origin;
  const fixture = async body => {
    const response = await fetch(base + '/__gap_fixture', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal(response.status, 200);
    return response.json();
  };
  const admin = await fixture({ project, tag, credentials });
  const api = async (path, method = 'GET', body) => {
    const response = await fetch(base + path, { method, headers: { Cookie: admin.cookie, 'X-CSRF-Token': admin.csrf, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    assert.ok(response.ok, 'Fixture HTTP status ' + response.status + ' for ' + path);
    return response.json();
  };
  await api('/api/agents', 'POST', { tag, project, role: 'implementer', machine_id: 'gap-fixture-machine' });
  const hostConfig = resolve(scratch, 'host.json');
  await writeFile(hostConfig, JSON.stringify({ base_url: base, machine_id: 'gap-fixture-machine' }), { mode: 0o600 });
  await chmod(hostConfig, 0o600);
  const cli = async (args, credential = credentials[0], expected = 0) => {
    try {
      const result = await exec('python3', ['-m', 'agent_hub_host.cli', ...args], { cwd: root, timeout: 30000, maxBuffer: 2 * 1024 * 1024,
        env: { ...process.env, PYTHONPATH: resolve(root, 'host'), PYTHONDONTWRITEBYTECODE: '1', HUB_URL: base,
          HUB_TOKEN: credential.token, HUB_JOB_TOKEN: '', HUB_CONFIG_FILE: hostConfig } });
      assert.equal(expected, 0, 'Expected CLI HOLD');
      return JSON.parse(result.stdout);
    } catch (error) {
      if (expected === 0 || error.code !== expected) throw new Error('CLI failed or unexpected result; private journal/evidence retained');
      assert.ok(error.stderr.includes('HOLD') || error.stderr.includes('held') || error.stderr.includes('operation failed'));
      return null;
    }
  };
  assert.equal((await cli(['conversations', 'participants'])).self.actor, actor);
  check('frozen production schema, public registration and actual Python CLI authenticated actor');
  const create = async label => (await api('/api/conversations', 'POST', { kind: 'group', title: 'Gap fixture ' + label,
    members: [actor], idempotency_key: 'gap-create-' + label + '-' + nonce })).conversation.id;
  const send = async (cid, seq) => api('/api/conversations/' + cid + '/messages', 'POST', {
    body: 'fixture nonce ' + nonce + ' sequence ' + seq, idempotency_key: 'gap-message-' + cid + '-' + seq });
  const history = cid => cli(['conversations', 'history', cid, '--after-seq', '0', '--limit', '100']);
  const bridgeArgs = cid => ['--enable-native', '--actor', actor, '--project', project, '--workspace', root,
    '--machine-id', 'gap-fixture-machine', '--conversation', cid];
  const cycle = cid => cli(['conversations', 'bridge', '--once', ...bridgeArgs(cid)]);
  const resumeArgs = (cid, seq) => ['bridge-resume', ...bridgeArgs(cid), '--resume-after-seq', String(seq), '--acknowledge-gap'];
  const prefix = await create('prefix');
  if (native) await cycle(prefix); // original journal starts at zero, no synthesized/reset state
  for (let seq = 1; seq <= 3; seq++) await send(prefix, seq);
  await fixture({ operation: 'expire', cid: prefix, seq: 1 });
  await fixture({ operation: 'expire', cid: prefix, seq: 2 });
  assert.deepEqual((await history(prefix)).messages.map(m => m.seq), [3]);
  check('real SQLite retention prunes prefix and leaves retained sequence 3');
  const interior = await create('interior');
  await send(interior, 1);
  if (native) await cycle(interior);
  await send(interior, 2);
  await send(interior, 3);
  await fixture({ operation: 'expire', cid: interior, seq: 2 });
  const interiorPage = await history(interior);
  assert.deepEqual(interiorPage.messages.map(m => m.seq), [1, 3]);
  assert.equal(interiorPage.gap_detected, false, 'Pinned API has imperfect interior-gap metadata');
  check('real HTTP/CLI reproduces interior retained 1,3 with imperfect gap metadata');
  if (native) {
    for (const cid of [prefix, interior]) {
      const before = await readFile(journal);
      await cli(['conversations', 'bridge', '--once', ...bridgeArgs(cid)], credentials[0], 1);
      assert.deepEqual(await readFile(journal), before);
      const local = JSON.parse(before);
      assert.equal(local.cursors[cid] || 0, cid === prefix ? 0 : 1);
    }
    check('genuine own native bridge defaults HOLD on prefix/interior gaps before delivery/read');
    let before = await readFile(journal);
    await cli(resumeArgs(prefix, 3), credentials[0], 1);
    assert.deepEqual(await readFile(journal), before);
    for (const credential of credentials.slice(1)) {
      await cli(resumeArgs(prefix, 2), credential, 1);
      assert.deepEqual(await readFile(journal), before);
    }
    await cli(resumeArgs(prefix, 4), credentials[0], 1);
    assert.deepEqual(await readFile(journal), before);
    check('actual CLI refuses retained/future range, revoked/scopeless/host/foreign credentials without journal changes');
    for (const cid of [prefix, interior]) {
      const old = JSON.parse(await readFile(journal));
      const readBefore = (await history(cid)).my_read_seq;
      const result = await cli(resumeArgs(cid, 2));
      assert.equal(result.status, 'gap_recorded');
      assert.equal(result.server_read_advanced, false);
      const saved = JSON.parse(await readFile(journal));
      assert.equal(saved.cursors[cid], 2);
      assert.deepEqual(saved.deliveries, old.deliveries);
      assert.deepEqual(saved.replies, old.replies);
      assert.equal((await history(cid)).my_read_seq, readBefore);
      before = await readFile(journal);
      await cli(resumeArgs(cid, 2), credentials[0], 1);
      assert.deepEqual(await readFile(journal), before);
      await cycle(cid); // fresh actual CLI process reloads the audit+cursor
      await cycle(cid);
      const state = JSON.parse(await readFile(journal));
      assert.equal(state.cursors[cid], 3);
      assert.equal((await history(cid)).my_read_seq, 3);
      assert.ok(!state.deliveries[cid + ':2']);
      const nativeLog = JSON.parse((await exec('a', ['--json', 'message', 'log', '--workspace', root], { cwd: root })).stdout);
      const delivered = Object.values(state.deliveries).filter(r => r.cid === cid);
      for (const row of delivered) {
        const matches = nativeLog.filter(m => m.id === row.native_id);
        assert.equal(matches.length, 1);
        assert.equal(matches[0].from.session_id, own.id);
        assert.equal(matches[0].to.session_id, own.id);
        assert.equal(matches[0].delivery, 'inbox');
        assert.equal(nativeLog.filter(m => m.body === row.body && JSON.stringify(m.data) === JSON.stringify(row.data)).length, 1);
      }
    }
    check('explicit prefix/interior recovery appends local audit without read/delivery claims; restart delivers retained 3 once');
    await api('/api/conversations/' + prefix + '/members/remove', 'POST', { actor });
    before = await readFile(journal);
    await cli(resumeArgs(prefix, 2), credentials[0], 1); // even cached/equal target revalidates current authority
    assert.deepEqual(await readFile(journal), before);
    check('actual removed membership refuses replay with journal preserved');
  }
  evidence = { frozen, candidate: (await exec('git', ['rev-parse', 'HEAD'], { cwd: root })).stdout.trim(),
    sourceBytes, sourceFiles: paths.length, bundleSha256: createHash('sha256').update(bundle.outputFiles[0].text).digest('hex'),
    checks, native: native ? { id: own.id, tag: own.tag, workspace: own.workspace, journal } : 'not requested',
    production: 'No deploy; local fixture credentials only; F7/M1/runtime acceptance remains open' };
  await writeFile(resolve(scratch, native ? 'native-evidence.json' : 'http-evidence.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 });
} finally {
  clearTimeout(deadline);
  await mf.dispose();
}
console.log('PASS ' + checks.length + ' bounded local fixture checks; evidence retained privately');
