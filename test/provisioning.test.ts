import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ProvisioningService } from '../src/provisioning';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
const admin = { caller: { isAdmin: true, kind: 'human' }, scopes: ['admin'] };
const host = { caller: { kind: 'host', machineId: 'test-machine' }, scopes: ['host'] };
const bundle = { label: 'test-account', provider: 'codex', version: '0.163.0', files: { '.codex/auth.json': 'synthetic-secret-marker' }, env: {}, args: [] };
const req = (path = '', method = 'GET', data?: any) => new Request('https://hub.test/api/engines' + path, { method, body: data ? JSON.stringify(data) : undefined });
async function fixture(fn: (s: ProvisioningService, sql: any, allow: (v: boolean) => void) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('provisioning-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state) => {
    let allowed = true;
    const s = new ProvisioningService(hub.sql, cb => state.storage.transactionSync(cb), { SESSION_SECRET: 'synthetic-session-secret-32-characters' } as any, hub.fleetService, () => allowed);
    s.init(); await fn(s, hub.sql, v => { allowed = v; });
  });
}
describe('central engine provisioning', () => {
  it('encrypts SQLite secrets, lists metadata only, enables routes only after exact host installation and removes them', async () => fixture(async (s, sql) => {
    const add = await s.handle(req('/test-account', 'PUT', bundle), admin);
    expect(add.status).toBe(200);
    const meta: any = await add.json();
    expect(JSON.stringify(sql.exec('SELECT * FROM engine_bundles').toArray())).not.toContain('synthetic-secret-marker');
    expect(await (await s.handle(req(), admin)).text()).not.toContain('synthetic-secret-marker');
    expect(sql.exec('SELECT * FROM fleet_routes').toArray()).toHaveLength(0);
    const sync: any = await (await s.handle(req('/sync'), host)).json();
    expect(sync.engines[0].files['.codex/auth.json']).toBe('synthetic-secret-marker');
    expect((await s.handle(req('/test-account', 'POST', { revision: 'old' }), host)).status).toBe(409);
    expect((await s.handle(req('/test-account', 'POST', { revision: meta.revision }), host)).status).toBe(200);
    expect(sql.exec('SELECT * FROM fleet_routes').toArray()).toHaveLength(1);
    expect((await s.handle(req('/test-account', 'DELETE'), admin)).status).toBe(200);
    for (const t of ['engine_bundles', 'engine_installations', 'fleet_routes', 'fleet_accounts']) expect(sql.exec('SELECT * FROM ' + t).toArray()).toHaveLength(0);
  }));
  it('denies unauthenticated, agents, stale host families and admin download of credentials', async () => fixture(async (s, _sql, allow) => {
    expect((await s.handle(req(), {})).status).toBe(401);
    for (const caller of [{ caller: {kind: 'agent'}, scopes: ['host'] }, admin]) expect((await s.handle(req('/sync'), caller)).status).toBe(403);
    expect((await s.handle(req('/test-account', 'PUT', bundle), host)).status).toBe(403);
    allow(false);
    expect((await s.handle(req('/sync'), host)).status).toBe(403);
    expect((await s.handle(req('/test-account', 'POST', { revision: 'x' }), host)).status).toBe(403);
  }));
  it('withdraws routes on rotation and rejects late acknowledgements', async () => fixture(async (s, sql) => {
    const first: any = await (await s.handle(req('/test-account', 'PUT', bundle), admin)).json();
    await s.handle(req('/test-account', 'POST', {revision: first.revision}), host);
    await s.handle(req('/test-account', 'PUT', bundle), admin);
    expect(sql.exec('SELECT * FROM fleet_routes').toArray()).toHaveLength(0);
    expect((await s.handle(req('/test-account', 'POST', {revision: first.revision}), host)).status).toBe(409);
  }));
  it('rejects traversal, arbitrary packages, environment injection and OpenCode GLM routes', async () => fixture(async s => {
    for (const data of [{ ...bundle, files: {'../escape': 'bad'} }, { ...bundle, env: { LD_PRELOAD: 'bad' } }, { ...bundle, provider: 'arbitrary' }, { ...bundle, provider: 'go', args: ['--model', 'glm'] }, { ...bundle, version: 'latest' }]) {
      expect((await s.handle(req('/test-account', 'PUT', data), admin)).status).toBe(400);
    }
  }));
  it('fails closed on ciphertext corruption and never deletes an unmanaged account', async () => fixture(async (s, sql) => {
    await s.handle(req('/test-account', 'PUT', bundle), admin);
    sql.exec("UPDATE engine_bundles SET ciphertext='broken'");
    expect((await s.handle(req('/sync'), host)).status).toBe(503);
    sql.exec("INSERT INTO fleet_accounts VALUES ('operator','codex',1,15,1)");
    await s.handle(req('/operator', 'DELETE'), admin);
    expect(sql.exec("SELECT * FROM fleet_accounts WHERE label='operator'").toArray()).toHaveLength(1);
  }));
});

it('keeps two Codex account profiles separate on two enrolled machines', async () => fixture(async (s, sql) => {
  const a: any = await (await s.handle(req('/test-account', 'PUT', bundle), admin)).json();
  const second = {...bundle, label: 'other-account', files: {'.codex/auth.json': 'different-private-marker'}};
  const b: any = await (await s.handle(req('/other-account', 'PUT', second), admin)).json();
  await s.handle(req('/test-account', 'POST', {revision: a.revision}), host);
  await s.handle(req('/other-account', 'POST', {revision: b.revision}), {caller: {...host.caller, machineId: 'second-machine'}, scopes:['host']});
  const routes = sql.exec('SELECT * FROM fleet_routes ORDER BY account').toArray();
  expect(routes).toEqual([
    {machine_id:'second-machine',engine:'codex',account:'other-account',profile:'hub-other-account'},
    {machine_id:'test-machine',engine:'codex',account:'test-account',profile:'hub-test-account'},
  ]);
  const downloaded: any = await (await s.handle(req('/sync'), host)).json();
  expect(downloaded.engines.find((b: any) => b.label === 'test-account').files['.codex/auth.json']).toBe('synthetic-secret-marker');
  expect(downloaded.engines.find((b: any) => b.label === 'other-account').files['.codex/auth.json']).toBe('different-private-marker');
}));

it('keeps Gemini login local, relays only native consent link and encrypts submitted codes', async () => fixture(async (s, sql) => {
  const google = {...bundle, provider:'gemini',auth_method:'machine-login',files:{},env:{}};
  const a: any = await (await s.handle(req('/test-account','PUT',google),admin)).json();
  expect((await s.handle(req('/test-account','POST',{revision:a.revision,authenticated:false}),host)).status).toBe(200);
  expect(sql.exec('SELECT * FROM fleet_routes').toArray()).toHaveLength(0);
  const login: any = await (await s.handle(req('/logins','POST',{label:'test-account',machine:'test-machine'}),admin)).json();
  const path='/logins/'+login.id;
  expect((await s.handle(req(path,'POST',{status:'waiting',url:'https://evil.invalid/'}),host)).status).toBe(400);
  expect((await s.handle(req(path,'POST',{status:'waiting',url:'https://accounts.google.com/o/oauth2/auth?state=synthetic'}),host)).status).toBe(200);
  expect((await s.handle(req(path,'POST',{code:'synthetic-onetime-code'}),admin)).status).toBe(200);
  expect(JSON.stringify(sql.exec('SELECT * FROM engine_logins').toArray())).not.toContain('synthetic-onetime-code');
  expect(await (await s.handle(req(),admin)).text()).not.toContain('synthetic-onetime-code');
  expect((await (await s.handle(req(path),host)).json() as any).code).toBe('synthetic-onetime-code');
  expect((await s.handle(req(path),{caller:{...host.caller,machineId:'other'},scopes:['host']})).status).toBe(403);
  await s.handle(req(path,'POST',{status:'authenticated'}),host);
  expect(sql.exec('SELECT code_ciphertext FROM engine_logins').one().code_ciphertext).toBeNull();
  await s.handle(req('/test-account','POST',{revision:a.revision,authenticated:true}),host);
  expect(sql.exec('SELECT * FROM fleet_routes').toArray()).toHaveLength(1);
}));

it('provisions central Z.ai accounts, isolates zcodex routes and supports multiple labels on one host', async () => fixture(async (s, sql) => {
  const zaiBundle1 = {
    label: 'zai-personal',
    provider: 'zai',
    version: '0.2.0',
    files: { '.zcode/v2/credentials.json': 'personal-zai-marker' },
    env: { ZAI_API_KEY: 'personal-key' },
    args: ['--model', 'glm-5.3-flash'],
  };
  const zaiBundle2 = {
    label: 'zai-work',
    provider: 'zai',
    version: '0.2.0',
    files: { '.zcode/v2/credentials.json': 'work-zai-marker' },
    env: { ZAI_API_KEY: 'work-key' },
    args: ['--model', 'glm-5.3-flash'],
  };
  const badZai = { ...zaiBundle1, args: ['--model', 'opencode-go/muse-spark-1.3-contributor'] };
  expect((await s.handle(req('/zai-personal', 'PUT', badZai), admin)).status).toBe(400);

  const a: any = await (await s.handle(req('/zai-personal', 'PUT', zaiBundle1), admin)).json();
  const b: any = await (await s.handle(req('/zai-work', 'PUT', zaiBundle2), admin)).json();

  const accounts = sql.exec("SELECT * FROM fleet_accounts WHERE provider='zai' ORDER BY label").toArray();
  expect(accounts).toEqual([
    { label: 'zai-personal', provider: 'zai', max_concurrency: 1, reserve_percent: 0, enabled: 1 },
    { label: 'zai-work', provider: 'zai', max_concurrency: 1, reserve_percent: 0, enabled: 1 },
  ]);

  const sync: any = await (await s.handle(req('/sync'), host)).json();
  expect(sync.engines.find((e: any) => e.label === 'zai-personal').files['.zcode/v2/credentials.json']).toBe('personal-zai-marker');
  expect(sync.engines.find((e: any) => e.label === 'zai-work').files['.zcode/v2/credentials.json']).toBe('work-zai-marker');

  await s.handle(req('/zai-personal', 'POST', { revision: a.revision }), host);
  await s.handle(req('/zai-work', 'POST', { revision: b.revision }), host);

  const routes = sql.exec("SELECT * FROM fleet_routes WHERE engine='codex' AND account LIKE 'zai-%' ORDER BY account").toArray();
  expect(routes).toEqual([
    { machine_id: 'test-machine', engine: 'codex', account: 'zai-personal', profile: 'hub-zai-personal' },
    { machine_id: 'test-machine', engine: 'codex', account: 'zai-work', profile: 'hub-zai-work' },
  ]);
}));
