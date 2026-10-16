import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { RoutingService, redact } from '../src/routing';
import { FleetService } from '../src/fleet';
import { modelCandidates, taskTier } from '../src/model-routing';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
const now = Date.now();
const at = (delta = 0) => new Date(now + delta).toISOString();
const admin = { caller: { isAdmin: true, actor: 'admin', kind: 'human' }, scopes: ['admin'] };
const client = (actor = 'client-a') => ({ caller: { actor, kind: 'agent' }, scopes: ['routing:submit'] });
const host = (machineId = 'hetzner') => ({ caller: { machineId, kind: 'host' }, scopes: ['host'] });
const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const input = (extra = {}) => ({ prompt: 'Check browser state', approved_action: 'Read state only', needs: [], workspace: 'tasks', idempotency_key: crypto.randomUUID(), ...extra });
const quota = { status: 'ok', observed_at: at(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: at(86400000) } } };
async function fixture(fn: (r: RoutingService, f: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('routing-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now); f.init();
    const r = new RoutingService(hub.sql, f, () => now); r.init();
    const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: at() } }), JSON.stringify({ engines: ['zcodex'], fleet_modes: ['headless'], workspaces: ['tasks'], cpu_count: 8 }));
    }
    const response = await f.handle(req('/api/fleet/accounts', { accounts: [{ label: 'shared-zai', provider: 'zai', max_concurrency: 3, reserve_percent: 0, enabled: true }], routes: ['hetzner','win35'].map(machine_id => ({ machine_id, engine: 'zcodex', account: 'shared-zai', profile: null })) }, 'PUT'), admin);
    expect(response.status).toBe(200);
    for (const machine of ['hetzner','win35']) {
      expect((await f.handle(req('/api/fleet/subscriptions', { subscriptions: [{ service: 'zai', account: 'shared-zai', plan: null, quota }] }, 'PUT'), host(machine))).status).toBe(200);
      expect((await r.handle(req('/api/routing/capabilities', { needs: machine === 'win35' ? ['browser:orin','phone-sms'] : [], max_agents: 2, runners: [{ engine: 'zcodex', profile: null, account: 'shared-zai', model: 'glm-5.3-flash', effort: 'high' }] }, 'PUT'), host(machine))).status).toBe(200);
    }
    await fn(r, f, sql);
  });
}
describe('capability task routing', () => {
  it('prefers Hetzner for daily work and preserves approved actions in private launch', () => fixture(async (r, f, sql) => {
    const response = await r.handle(req('/api/tasks', input()), client()); expect(response.status).toBe(201);
    const job: any = await response.json(); expect(job.machine).toBe('hetzner'); expect(job.tier).toBe('daily'); expect(job.selection.model).toBe('glm-5.3-flash');
    expect(JSON.stringify(job)).not.toContain('Read state only');
    expect(sql.exec('SELECT prompt FROM fleet_launches').one().prompt).toContain('Approved action: Read state only');
  }));
  it('routes browser + phone batches as one launch on win35', () => fixture(async (r, f, sql) => {
    const data: any = input({ needs: ['browser:orin','phone-sms'], tasks: [{ prompt: 'A', approved_action: 'Read A' }, { prompt: 'B', approved_action: 'Read B' }] }); delete data.prompt; delete data.approved_action;
    const job: any = await (await r.handle(req('/api/tasks', data), client())).json(); expect(job.machine).toBe('win35'); expect(job.task_count).toBe(2);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));
  it('queues missing capabilities, respects tier, and retries when hosts advertise', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input({ needs: ['browser:main'] })), client())).json(); expect(job.launch_id).toBeNull();
    sql.exec("UPDATE routing_machines SET needs_json = '[\"browser:main\"]' WHERE machine_id = 'win35'");
    await r.dispatch(); const result: any = await (await r.handle(req('/api/tasks/' + job.id), client())).json(); expect(result.machine).toBe('win35');
    const quick: any = await (await r.handle(req('/api/tasks', input({ tier: 'quick' })), client())).json(); expect(quick.launch_id).toBeNull();
  }));
  it('isolates client jobs and rejects result injection', () => fixture(async (r) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    expect((await r.handle(req('/api/tasks/' + job.id), client('other'))).status).toBe(404);
    expect((await r.handle(req('/api/tasks/' + job.id + '/result', { status: 'done', report: 'x', artifacts: [], confirmed_exit: true }), client())).status).toBe(403);
    expect((await r.handle(req('/api/tasks/' + job.id + '/result', {}), host('win35'))).status).toBe(404);
  }));
  it('deduplicates retries and rejects changed payloads', () => fixture(async (r, f, sql) => {
    const data = input(); const a: any = await (await r.handle(req('/api/tasks', data), client())).json();
    const b: any = await (await r.handle(req('/api/tasks', data), client())).json(); expect(a.id).toBe(b.id);
    expect((await r.handle(req('/api/tasks', { ...data, prompt: 'changed' }), client())).status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));
  it('holds human waits and releases only on confirmed terminal results, redacting artifacts', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", job.launch_id);
    const path = '/api/tasks/' + job.id + '/result';
    const result = { status: 'waiting-on-human', report: 'captcha; OTP=123456; https://auth.test/private', artifacts: [{ name: 'log', text: 'token=secret-value' }] };
    const wait: any = await (await r.handle(req(path, result), host())).json(); expect(wait.status).toBe('waiting-on-human'); expect(JSON.stringify(wait)).not.toContain('123456'); expect(JSON.stringify(wait)).not.toContain('secret-value');
    expect((await r.handle(req(path, { ...result, status: 'done' }), host())).status).toBe(409);
    expect((await r.handle(req(path, { ...result, status: 'done', confirmed_exit: true }), host())).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', job.launch_id).one().state).toBe('exited');
    expect((await r.handle(req(path, { ...result, status: 'running' }), host())).status).toBe(409);
  }));
  it('rejects unauthenticated, over-size, missing approval and invalid tiers', () => fixture(async (r) => {
    expect((await r.handle(req('/api/tasks'), {})).status).toBe(401);
    for (const data of [input({ approved_action: '' }), input({ tier: 'imaginary' }), input({ prompt: 'x'.repeat(40000) })]) expect((await r.handle(req('/api/tasks', data), client())).status).toBe(400);
  }));
  it('issues only scoped per-client tokens to admins and revokes without exposing existing values', () => fixture(async (r, f, sql) => {
    expect((await r.handle(req('/api/routing/clients/bot', {}), client())).status).toBe(403);
    const issued: any = await (await r.handle(req('/api/routing/clients/bot', {}), admin)).json(); expect(issued.token).toMatch(/^hubt_/);
    const credential = sql.exec("SELECT * FROM credentials WHERE actor = 'routing-client:bot'").one(); expect(credential.token_hash).not.toBe(issued.token); expect(credential.scopes).toBe('["routing:submit"]');
    expect((await r.handle(req('/api/routing/clients/bot', undefined, 'DELETE'), admin)).status).toBe(200);
  }));
});
describe('configured model tiers and ranking', () => {
  it('keeps aliases and never substitutes tiers', () => { expect(taskTier({tier:'smart'})).toBe('deep'); expect(taskTier({difficulty:'concrete'})).toBe('quick'); });
  it('blocks Claude, requires explicit Codex, and excludes full GLM and GLM in OpenCode', () => {
    const route = { provider: 'codex', engine: 'codex', profile: null, account: 'a' };
    const ads = [{ engine:'codex',profile:null,account:'a',model:'gpt-6.1-sol',effort:'high' }];
    expect(modelCandidates({}, route, ads, quota, now)).toHaveLength(0);
    expect(modelCandidates({preferred_runner:'codex'}, route, ads, quota, now)).toHaveLength(1);
    expect(modelCandidates({}, {provider:'claude',engine:'claude',profile:null,account:'a'}, [{engine:'claude',profile:null,account:'a',model:'sonnet',effort:'high'}], quota, now)).toHaveLength(0);
    expect(modelCandidates({}, {provider:'go',engine:'opencode',profile:null,account:'a'}, [{engine:'opencode',profile:null,account:'a',model:'glm-5.3',effort:null}], quota, now)).toHaveLength(0);
  });
  it('redacts codes, auth URLs, bearer and named secrets', () => { expect(redact('SMS code: 123456; Bearer abc; https://auth.test/code?token=x; sk-abcdef')).not.toMatch(/123456|abc|https:/); });
});

describe('authenticated fleet task HTTP integration', () => {
  it('uses the existing Hub authentication, scoped issuance and revocation end to end', async () => {
    const { SELF } = await import('cloudflare:test');
    const { sha256 } = await import('../src/auth/crypto');
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hub'));
    const token = 'hubt_' + crypto.randomUUID();
    await runInDurableObject(stub, async (hub: Hub) => {
      const id = crypto.randomUUID();
      hub.sql.exec('INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)', id, 'human', await sha256(token), id, 'admin-test', 'admin-test', '["admin"]', 'test', at(), at(86400000));
    });
    const name = 'http-' + crypto.randomUUID();
    const adminRequest = (method: string, path: string, data?: any) => new Request('https://hub.test' + path, { method, headers: { Authorization: 'Bearer ' + token }, body: data === undefined ? undefined : JSON.stringify(data) });
    expect((await SELF.fetch('https://hub.test/api/fleet/tasks')).status).toBe(401);
    const issued = await SELF.fetch(adminRequest('POST', '/api/fleet/routing/clients/' + name, {}));
    expect(issued.status).toBe(201); const credential: any = await issued.json();
    const clientRequest = (method: string, path: string, data?: any) => new Request('https://hub.test' + path, { method, headers: { Authorization: 'Bearer ' + credential.token }, body: data === undefined ? undefined : JSON.stringify(data) });
    const submitted = await SELF.fetch(clientRequest('POST', '/api/fleet/tasks', input({ needs: ['unavailable'] })));
    expect(submitted.status).toBe(201); const job: any = await submitted.json(); expect(job.status).toBe('queued');
    expect((await SELF.fetch(clientRequest('GET', '/api/fleet/tasks/' + job.id))).status).toBe(200);
    expect((await SELF.fetch(clientRequest('POST', '/api/fleet/launches'))).status).toBe(403);
    expect((await SELF.fetch(adminRequest('DELETE', '/api/fleet/routing/clients/' + name))).status).toBe(200);
    expect((await SELF.fetch(clientRequest('GET', '/api/fleet/tasks'))).status).toBe(401);
  });
});

describe('redaction of structured output', () => {
  it('redacts quoted JSON keys and arbitrary bearer/JWT-shaped blobs', () => {
    const result = redact(JSON.stringify({ token: 'short-secret', password: 'multi word password', api_key: 'key-value', nested: { secret: 'private' } }));
    for (const value of ['short-secret', 'multi word password', 'key-value', 'private']) expect(result).not.toContain(value);
  });
});

describe('routing recovery, capacity and private artifacts', () => {
  it('recovers an interrupted job/link write without a duplicate launch', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    sql.exec('UPDATE routing_jobs SET launch_id = NULL WHERE id = ?', job.id);
    await r.dispatch();
    const recovered: any = await (await r.handle(req('/api/tasks/' + job.id), client())).json();
    expect(recovered.launch_id).toBe(job.launch_id); expect(recovered.selection.model).toBe('glm-5.3-flash');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));
  it('reserves machine concurrency inside fleet placement and enriches offers', () => fixture(async (r, f, sql) => {
    sql.exec('UPDATE routing_machines SET max_agents = 1');
    const first: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    const second: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    const third: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    expect(first.machine).toBe('hetzner'); expect(second.machine).toBe('win35'); expect(third.launch_id).toBeNull();
    const offered: any = await (await f.handle(req('/api/fleet/offers'), host())).json();
    expect(offered.offers[0].task_id).toBe(first.id); expect(offered.offers[0].selection.model).toBe('glm-5.3-flash');
  }));
  it('requires assigned-host sanitization and protects screenshot contents by job ownership', () => fixture(async (r) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    const path = '/api/tasks/' + job.id + '/artifacts';
    const image = { name: 'screenshot', media_type: 'image/png', data_base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aBZkAAAAASUVORK5CYII=', sanitized: true };
    expect((await r.handle(req(path, { ...image, sanitized:false }), host())).status).toBe(400);
    expect((await r.handle(req(path, image), host('win35'))).status).toBe(404);
    expect((await r.handle(req(path, image), client())).status).toBe(403);
    expect((await r.handle(req(path, image), host())).status).toBe(201);
    expect((await r.handle(req(path + '/screenshot'), client('another'))).status).toBe(404);
    const artifact: any = await (await r.handle(req(path + '/screenshot'), client())).json(); expect(artifact.data_base64).toBe(image.data_base64);
    const listed: any = await (await r.handle(req('/api/tasks/' + job.id), client())).json();
    expect(listed.artifacts).toContainEqual({name:'screenshot',media_type:'image/png'}); expect(JSON.stringify(listed)).not.toContain(image.data_base64);
  }));
});
