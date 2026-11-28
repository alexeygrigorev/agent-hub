// Intake bf07f437: `hub host-release` against the real Node Hub2 server and the
// real Python CLI. Hub2 has no per-machine pin route (the Hub1 PUT answered a
// bare 404); the CLI must report rollout state from the published
// /cli/host-release.json or name the cause and the deploy path.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createFixture, makeTempDir } from './node-fixture.mjs';
const { default: entry, ControlDO, ProjectDO, MachineDO, AccountDO, MailboxDO } = await import('../production-worker.ts');
const root = resolve(import.meta.dirname, '../..');
const token = 'synthetic-host-release-operator-0000000';
const hash = value => createHash('sha256').update(value).digest('hex');
class SeedControl extends ControlDO {
  constructor(ctx, env) { super(ctx, env); this.ctx = ctx; }
  async fetch(r) {
    if (!this.seeded) {
      await super.fetch(new Request('https://internal/_metrics'));
      this.ctx.storage.sql.exec('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        'credential:hub-cli', 'operator', 'human:cli-user', 'gen-cli', hash(token), JSON.stringify(['operator:admin']), 'family-cli', 'hub-cli', Date.now(), Date.now() + 3600000, 'cli@example.test', 'synthetic-csrf');
      this.seeded = true;
    }
    return super.fetch(r);
  }
}
const published = 'b97a9c3e974f5f408f9d1b5b2da72d2c51ff68c8';
const older = '8190f106528476a33447d63295c3a42ca7aa750d';
const scratch = await makeTempDir('cli-host-release-');
const assetsDir = resolve(scratch, 'public');
await mkdir(resolve(assetsDir, 'cli'), { recursive: true });
const manifestPath = resolve(assetsDir, 'cli', 'host-release.json');
await writeFile(manifestPath, JSON.stringify({ schema_version: 1, host_version: published, protocol_version: 1, artifacts: {} }));
const fixture = await createFixture({ root, entry, assetsDir,
  classes: { CONTROL: SeedControl, PROJECTS: ProjectDO, MACHINES: MachineDO, ACCOUNTS: AccountDO, MAILBOXES: MailboxDO },
  env: { HUB2_RELEASE_MODE: 'active', HUB_ADMINS: 'cli@example.test' },
});
const config = resolve(scratch, 'cli.json');
await writeFile(config, JSON.stringify({ client_kind: 'human', access_token: token, base_url: fixture.origin }), { mode: 0o600 });
// Minimal environment: never inherit a worker launch context or Hub credential from the caller.
const env = { PATH: process.env.PATH, LANG: 'C.UTF-8', HOME: scratch, XDG_CONFIG_HOME: scratch, HUB_CONFIG_FILE: config, PYTHONPATH: resolve(root, 'host') };
function cli(...args) {
  const child = spawn('python3', ['-m', 'agent_hub_host.cli', ...args], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '', err = '';
  child.stdout.on('data', b => out += b); child.stderr.on('data', b => err += b);
  const timer = setTimeout(() => child.kill(), 15000);
  return new Promise((done, reject) => { child.once('error', reject); child.once('close', code => { clearTimeout(timer); done({ code, out, err }); }); });
}
try {
  // The server really has no pin route: this is the bare 404 operators saw.
  const raw = await fixture.fetch('/api/fleet/routing/host-release/hetzner-aplexer', { method: 'PUT', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify({ sha: published }) });
  assert.equal(raw.status, 404);
  assert.equal((await fixture.fetch('/cli/host-release.json')).status, 200);

  const stale = await cli('host-release', '--machine', 'hetzner-aplexer', '--sha', older);
  assert.equal(stale.code, 1, stale.err);
  assert.match(stale.err, /host_release_not_published/);
  assert.match(stale.err, new RegExp(`publishes ${published} not ${older}`));
  assert.match(stale.err, /Hub 2 production/);
  assert.match(stale.err, /gh workflow run deploy\.yml --ref main -f dry_run=false/);
  assert.doesNotMatch(stale.err, /status 404|not_found/);
  assert.doesNotMatch(stale.err + stale.out, new RegExp(token));

  const live = await cli('host-release', '--machine', 'hetzner-aplexer', '--sha', published.toUpperCase());
  assert.equal(live.code, 0, live.err);
  const body = JSON.parse(live.out);
  assert.equal(body.published_host_version, published);
  assert.equal(body.requested_sha, published);
  assert.equal(body.state, 'published');
  assert.equal(body.machine_view_error, 'machine_not_listed');

  const cleared = await cli('host-release', '--machine', 'hetzner-aplexer', '--clear');
  assert.equal(cleared.code, 1);
  assert.match(cleared.err, /not_supported_on_hub2; Hub2 has no per-machine host-release pin to clear/);

  const launcher = await cli('launcher-release', '--machine', 'hetzner-aplexer', '--sha', published);
  assert.equal(launcher.code, 1);
  assert.match(launcher.err, /not_supported_on_hub2; \/api\/fleet\/routing\/launcher-release\/hetzner-aplexer is not served by Hub2/);

  await rm(manifestPath);
  const missing = await cli('host-release', '--machine', 'hetzner-aplexer', '--sha', published);
  assert.equal(missing.code, 1);
  assert.match(missing.err, /host_release_manifest_unavailable; could not read http:\/\/127\.0\.0\.1:\d+\/cli\/host-release\.json/);
  console.log('hub host-release names its cause against the real Node Hub2 server: passed');
} finally {
  await fixture.dispose(); await rm(fixture.dataDir, { recursive: true, force: true }); await rm(scratch, { recursive: true, force: true });
}
