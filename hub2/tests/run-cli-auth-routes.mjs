// Real Node/SQLite routes plus the installed Python CLI, with synthetic credentials.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { writeFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createFixture, makeTempDir } from './node-fixture.mjs';
const { default: entry, ControlDO, ProjectDO, MachineDO, AccountDO, MailboxDO } = await import('../production-worker.ts');
const root = resolve(import.meta.dirname, '../..');
const token = 'synthetic-cli-human-credential-000000000';
const browser = 'synthetic-browser-credential-0000000000';
const machine = 'synthetic-machine-credential-0000000000';
const hash = value => createHash('sha256').update(value).digest('hex');
class SeedControl extends ControlDO {
  constructor(ctx, env) { super(ctx, env); this.ctx = ctx; }
  async fetch(r) {
    if (!this.seeded) {
      await super.fetch(new Request('https://internal/_metrics'));
      for (const [key, kind, resource, client] of [[token, 'operator', 'human:cli-user', 'hub-cli'], [browser, 'operator', 'human:cli-user', 'browser'], [machine, 'machine', 'a-empty', 'host']]) {
        this.ctx.storage.sql.exec('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
          'credential:' + client, kind, resource, 'gen-' + client, hash(key), JSON.stringify(['operator:admin']), 'family-' + client, client, Date.now(), Date.now() + 3600000, 'cli@example.test', 'synthetic-csrf');
      }
      this.ctx.storage.sql.exec("INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at) VALUES('credential:b','machine','b-full','gen-b','machine-hash-b','[]','family-b','host-b',?)", Date.now());
      this.seeded = true;
    }
    return super.fetch(r);
  }
}
class SeedMachine extends MachineDO {
  constructor(ctx, env) { super(ctx, env); this.ctx = ctx; }
  async fetch(r) {
    if (!this.seeded && this.ctx.id?.name !== 'a-empty') {
      // Node IDs are not required by the DO contract; use the target query below
      // to seed only the second host through the test-only namespace wrapper.
      if (r.headers.get('x-fixture-full') === 'yes') {
        this.ctx.storage.sql.exec("INSERT INTO sessions(id,operation_id,request_digest,dispatch_id,attempt,project_id,intake_id,machine_id,epoch,title,acceptance_criteria_json,launch_json,state,created_at,updated_at) VALUES('session-1','op-1','digest','dispatch',1,'owner/repo','intake','b-full',1,'CLI regression','[]','{}','running',?,?)", Date.now(), Date.now());
        this.ctx.storage.sql.exec("INSERT INTO sessions(id,operation_id,request_digest,dispatch_id,attempt,project_id,intake_id,machine_id,epoch,title,acceptance_criteria_json,launch_json,state,created_at,updated_at) VALUES('session-2','op-2','digest','dispatch',1,'owner/repo','intake','b-full',1,'CLI regression','[]','{}','running',?,?)", Date.now(), Date.now());
        this.seeded = true;
      }
    }
    return super.fetch(r);
  }
}
const wrappedEntry = { fetch(r, env) {
  const machines = env.MACHINES;
  const MACHINES = { idFromName: name => machines.idFromName(name), get: id => ({ fetch(input, init) {
    const request = new Request(input, init);
    if (id.name === 'b-full') request.headers.set('x-fixture-full', 'yes');
    return machines.get(id).fetch(request);
  } }) };
  return entry.fetch(r, { ...env, MACHINES });
} };
const fixture = await createFixture({ root, entry: wrappedEntry,
  classes: { CONTROL: SeedControl, PROJECTS: ProjectDO, MACHINES: SeedMachine, ACCOUNTS: AccountDO, MAILBOXES: MailboxDO },
  env: { HUB2_RELEASE_MODE: 'active', HUB_ADMINS: 'cli@example.test' },
});
const headers = { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' };
const call = (path, method = 'GET', body) => fixture.fetch(path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
const scratch = await makeTempDir('cli-auth-routes-');
async function command(program, args, env = {}) {
  const child = spawn(program, args, { cwd: root, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '', err = '';
  child.stdout.on('data', b => out += b); child.stderr.on('data', b => err += b);
  const timer = setTimeout(() => child.kill(), 15000);
  try { assert.equal(await new Promise((done, reject) => { child.once('error', reject); child.once('close', done); }), 0, err); return out; }
  finally { clearTimeout(timer); }
}
try {
  assert.equal((await fixture.fetch('/api/auth/me')).status, 401);
  const me = await call('/api/auth/me'); assert.equal(me.status, 200);
  assert.match(me.headers.get('Cache-Control'), /no-store/);
  assert.deepEqual(await me.json(), { actor: 'human:cli-user', kind: 'human', sub: 'cli-user', email: 'cli@example.test', label: 'hub-cli', scopes: ['operator:admin'], isAdmin: true });
  assert.equal((await fixture.fetch('/api/auth/me', { headers: { Cookie: 'hub2_session=' + browser } })).status, 200);
  assert.equal((await fixture.fetch('/api/auth/me', { headers: { Cookie: 'hub2_session=' + browser, Authorization: 'Bearer invalid' } })).status, 401);
  assert.equal((await fixture.fetch('/api/auth/me', { headers: { Authorization: 'Bearer ' + machine } })).status, 403);
  assert.equal((await call('/api/quota?view=fleet')).status, 200);
  assert.deepEqual((await (await call('/api/quota?view=fleet')).json()).pools, []);
  const candidates = [{ id: 'codex', engine: 'codex', model: 'synthetic', account_id: 'test-account', weight: 1, capabilities: [], enabled: true }];
  assert.equal((await call('/api/model-policy', 'PUT', { candidates })).status, 200);
  assert.equal((await call('/api/accounts/test-account/api/account', 'PATCH', { provider: 'codex', enabled: true })).status, 200);
  let pools = (await (await call('/api/quota?view=fleet')).json()).pools;
  assert.equal(pools[0].state, 'unknown'); assert.equal(pools[0].remaining_percent, null);
  const now = Date.now();
  assert.equal((await call('/api/accounts/test-account/api/account/quota', 'PUT', { remaining: 72, captured_at: now, valid_until: now + 60000, source: 'synthetic' })).status, 200);
  pools = (await (await call('/api/quota?view=fleet&provider=codex&engine=codex&account=test-account')).json()).pools;
  assert.equal(pools[0].remaining_percent, 72); assert.equal(pools[0].state, 'fresh');
  assert(!JSON.stringify(pools).includes('synthetic'), 'quota does not leak raw evidence');
  assert.deepEqual((await (await call('/api/quota?provider=claude')).json()).pools, []);
  assert.equal((await call('/api/quota?host_id=b-full')).status, 400);
  assert.equal((await fixture.fetch('/api/quota')).status, 401);

  // Empty first host must still return a cursor to the populated second host.
  const first = await (await call('/api/sessions?limit=1')).json();
  assert.deepEqual(first.sessions, []); assert(first.next_cursor);
  const second = await (await call('/api/sessions?limit=1&cursor=' + first.next_cursor)).json();
  assert.equal(second.sessions[0].id, 'session-1'); assert(second.next_cursor);
  const third = await (await call('/api/sessions?limit=1&cursor=' + second.next_cursor)).json();
  assert.equal(third.sessions[0].id, 'session-2'); assert.equal(third.next_cursor, null);
  assert.equal((await (await call('/api/sessions?machine_id=b-full&limit=1')).json()).sessions.length, 1);
  assert.equal((await call('/api/sessions?limit=100')).status, 200);
  // Canonical repo import is PATCH; list is a paginated items/next contract.
  assert.equal((await call('/api/repos/owner%2Frepo', 'PATCH', { canonical_url: 'https://github.com/owner/repo', purpose: 'CLI route test', aliases: ['repo'] })).status, 200);
  assert.equal((await (await call('/api/repos')).json()).items[0].canonical_name, 'owner/repo');

  // POST /api/sessions reports validation refusals clearly and includes the selected route on acceptance.
  const start = (body) => call('/api/sessions', 'POST', body);
  const base = { key: 'start-test-0001', workspace: '/tmp/ws', mode: 'headless', brief: 'do the thing', repo: 'owner/repo' };
  for (const [patch, status, code] of [
    [{ key: undefined }, 400, 'idempotency_key_required'],
    [{ brief: undefined }, 400, 'brief_required'],
    [{ purpose: 'Review' }, 400, 'invalid_purpose'],
    [{ purpose: null }, 400, 'invalid_purpose'],
    [{ repo: 'not-a-repo-name' }, 404, 'project_not_found_or_forbidden'],
  ]) {
    const refused = await start({ ...base, ...patch });
    assert.equal(refused.status, status); assert.equal((await refused.json()).error, code);
  }
  const noWorkspace = await start({ ...base, key: 'start-no-workspace', workspace: undefined, repo: undefined });
  const noWorkspaceBody = await noWorkspace.json();
  assert.equal(noWorkspace.status, 400); assert.equal(noWorkspaceBody.error, 'workspace_required');
  assert.match(noWorkspaceBody.detail, /--repo owner\/name/);
  assert.match(noWorkspaceBody.detail, /--workspace <registered label/);
  // --repo owner/name supplies its repository workspace when --workspace is omitted.
  const repoOnly = await start({ ...base, key: 'start-repo-only-01', workspace: undefined });
  const repoOnlyBody = await repoOnly.json();
  assert([201, 202].includes(repoOnly.status), 'repo-only start status ' + repoOnly.status);
  assert.equal(repoOnlyBody.project, 'owner/repo');
  assert(repoOnlyBody.id && repoOnlyBody.state, 'repo-only accepted start has session identity and state');
  // A configured workspace label is accepted and left for the placed host to resolve under its roots.
  const byLabel = await start({ ...base, key: 'start-workspace-label-01', workspace: 'repo', repo: undefined });
  assert([201, 202].includes(byLabel.status), 'workspace-label start status ' + byLabel.status);
  assert.equal((await byLabel.json()).project, 'owner/repo');
  // --intake-task-id resolves the project from the intake itself (no --repo, no default project).
  const made = await call('/api/intakes?project=owner%2Frepo', 'POST', { key: 'intake-for-start-1', title: 't', text: 'x', acceptance_criteria: ['done'] });
  assert.equal(made.status, 201); const intakeId = (await made.json()).intake.id;
  const byTask = await start({ key: 'start-by-task-01', workspace: '/tmp/ws', mode: 'headless', intake_task_id: intakeId.slice(0, 8) });
  const byTaskBody = await byTask.json();
  assert.notEqual(byTaskBody.error, 'project_required'); assert.notEqual(byTask.status, 501);
  if (byTaskBody.intake_id) assert.equal(byTaskBody.intake_id, intakeId);
  const missingTask = await start({ key: 'start-by-task-02', workspace: '/tmp/ws', mode: 'headless', intake_task_id: 'deadbeef00' });
  assert.equal(missingTask.status, 404);
  // An admin's first start for an unregistered owner/name registers the repo instead of refusing.
  assert.equal((await call('/api/repos/owner%2Fzzz-new')).status, 404);
  await start({ ...base, key: 'start-autoreg-001', repo: 'owner/zzz-new' });
  assert.equal((await call('/api/repos/owner%2Fzzz-new')).status, 200);
  assert.equal((await fixture.fetch('/api/sessions', { method: 'POST', body: '{}' })).status, 401);
  // No machine is connected: Hub still accepts the start with a selected model and queues it for the host.
  const astra = { id: 'review-route', engine: 'codex', model: 'gpt-6-astra', effort: 'medium', account_id: 'test-account', weight: 1, capabilities: ['review-only'], enabled: true };
  const invalidPolicy = await call('/api/model-policy', 'PUT', { candidates: [{ ...astra, capabilities: [] }] });
  assert.equal(invalidPolicy.status, 400);
  assert.equal((await invalidPolicy.json()).error, 'astra_review_only_required');
  assert.equal((await call('/api/model-policy', 'PUT', { candidates: [astra] })).status, 200);
  const astraDenied = await start({ ...base, key: 'start-astra-denied', model: 'review-route', engine_override_reason: 'Purpose guard test' });
  assert.equal(astraDenied.status, 400); assert.equal((await astraDenied.json()).error, 'astra_not_for_implementation');
  const unplaced = await start({ ...base, purpose: 'review', model: 'review-route', engine_override_reason: 'Purpose guard test' }); const unplacedBody = await unplaced.json();
  assert([201, 202].includes(unplaced.status), 'offline-host start status ' + unplaced.status);
  assert(unplacedBody.id && unplacedBody.engine && unplacedBody.model, 'accepted start reports the chosen engine+model');
  assert.equal(unplacedBody.model, 'gpt-6-astra', 'public start preserves explicit review purpose');
  assert.equal((await call('/api/model-policy', 'PUT', { candidates })).status, 200);

  const config = resolve(scratch, 'cli.json');
  await writeFile(config, JSON.stringify({ client_kind: 'human', access_token: token, base_url: fixture.origin }), { mode: 0o600 });
  const cliEnv = { HOME: scratch, XDG_CONFIG_HOME: scratch, HUB_CONFIG_FILE: config, PYTHONPATH: resolve(root, 'host'), HUB2_TOKEN: '', HUB_TOKEN: '' };
  const cli = (...args) => command('python3', ['-m', 'agent_hub_host.cli', ...args], cliEnv);
  assert.match(await cli('auth', 'status'), /Status: Logged in as human\./);
  assert.equal(JSON.parse(await cli('auth', 'status', '--json')).isAdmin, true);
  assert.match(await cli('quota'), /72%/);
  const listed = JSON.parse(await cli('sessions', 'list', '--json'));
  assert.equal(listed.sessions.length, 7, 'two seeded sessions and five accepted starts are listed');
  assert.equal(JSON.parse(await cli('repos', 'list')).items[0].canonical_name, 'owner/repo');
  // curl exercises the same HTTP front used by the CLI.
  assert.equal(JSON.parse(await command('curl', ['-fsS', '-H', 'Authorization: Bearer ' + token, fixture.origin + '/api/auth/me'])).actor, 'human:cli-user');
  console.log('CLI auth/profile, quota, session pagination, canonical repo import and real Python CLI: passed');
} finally {
  await fixture.dispose(); await rm(fixture.dataDir, { recursive: true, force: true }); await rm(scratch, { recursive: true, force: true });
}

// Bootstrap must expose the same read/update routes while dispatch stays gated.
const assets = [];
const fake = { HUB2_RELEASE_MODE: 'bootstrap', ASSETS: { fetch: async r => { assets.push(new URL(r.url).pathname); return Response.json({ version: 'synthetic' }); } } };
for (const path of ['/cli/manifest.json', '/cli/hub.pyz', '/cli/hub.pyz.sha256', '/cli/install.sh', '/cli/install.ps1']) {
  assert.equal((await entry.fetch(new Request('https://hub.test' + path), fake)).status, 200);
}
assert.equal(assets.length, 5);
assert.equal((await entry.fetch(new Request('https://hub.test/cli/private.json'), fake)).status, 503);
console.log('Public CLI artifact allowlist in bootstrap: passed');
