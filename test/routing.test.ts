import {freshCpu} from './cpu-fixture';
import { describe, it, expect, vi } from 'vitest';
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
    const r = new RoutingService(hub.sql, f, () => now, cb => state.storage.transactionSync(cb)); r.init();
    const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at()), gate: { launch_allowed: true, at: at() } }), JSON.stringify({ engines: ['zcodex'], fleet_modes: ['headless'], workspaces: ['tasks'], cpu_count: 8 }));
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
  it('dispatches with central account quota when every machine subscription is missing', () => fixture(async (r, f, sql) => {
    sql.exec('DELETE FROM fleet_subscriptions');
    sql.exec('INSERT INTO server_quota_snapshots VALUES (?, ?, ?, ?)', 'shared-zai', 'zai', at(), JSON.stringify(quota));
    const response = await r.handle(req('/api/tasks', input()), client());
    expect(response.status).toBe(201);
    const job: any = await response.json();
    expect(job.machine).toBe('hetzner');
    expect(job.selection.model).toBe('glm-5.3-flash');
  }));

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
  it('blocks Grok, requires explicit Codex, and excludes full GLM and GLM in OpenCode', () => {
    const route = { provider: 'codex', engine: 'codex', profile: null, account: 'a' };
    const ads = [{ engine:'codex',profile:null,account:'a',model:'gpt-6.1-sol',effort:'high' }];
    expect(modelCandidates({}, route, ads, quota, now)).toHaveLength(0);
    expect(modelCandidates({preferred_runner:'codex'}, route, ads, quota, now)).toHaveLength(1);
    expect(modelCandidates({}, {provider:'grok',engine:'grok',profile:null,account:'a'}, [{engine:'grok',profile:null,account:'a',model:'grok',effort:null}], quota, now)).toHaveLength(0);
    expect(modelCandidates({}, {provider:'claude',engine:'claude',profile:null,account:'a'}, [{engine:'claude',profile:null,account:'a',model:'sonnet',effort:'high'}], quota, now)).toHaveLength(1);
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
      hub.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('admin-test', 'admin@fake.local', 1, ?, ?)", at(), at());
      hub.sql.exec('INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)', id, 'human', await sha256(token), id, 'admin@fake.local', 'human:admin-test', '["admin"]', 'admin-test', at(), at(86400000));
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

describe('account pinning and existing native occupancy', () => {
  it('pins the eligible model account when a richer account on the same runner is unsupported', () => fixture(async (r, f, sql) => {
    sql.exec("INSERT INTO fleet_accounts VALUES ('richer-unsupported', 'zai', 3, 0, 1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('hetzner', 'zcodex', 'richer-unsupported', NULL)");
    const richer = { ...quota, windows: { weekly: { present: true, percent_remaining: 95, reset_at: at(86400000) } } };
    sql.exec("INSERT INTO fleet_subscriptions VALUES ('hetzner', 'richer-unsupported', 'zai', NULL, ?, ?)", at(), JSON.stringify(richer));
    const job: any = await (await r.handle(req('/api/tasks', input({ preferred_machine:'hetzner' })), client())).json();
    const launch = sql.exec('SELECT account FROM fleet_launches WHERE id = ?', job.launch_id).one();
    expect(launch.account).toBe('shared-zai');
  }));
  it('counts existing unmanaged sessions against machine and account capacity', () => fixture(async (r, f, sql) => {
    sql.exec("UPDATE routing_machines SET max_agents = 1 WHERE machine_id = 'hetzner'");
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account) VALUES ('hetzner','existing','existing','zcodex','running',?,'tasks','shared-zai')", at());
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    expect(job.machine).toBe('win35');
    const account = f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-zai'").one());
    expect(account.active).toBe(2);
    sql.exec('UPDATE fleet_launches SET native_id = ? WHERE id = ?', 'existing', job.launch_id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account) VALUES ('win35','existing','linked','zcodex','running',?,'tasks','shared-zai')", at());
    // The linked session deduplicates on win35; hetzner's independent native process still counts.
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-zai'").one()).active).toBe(2);
  }));
  it('releases long-idle unlinked observed sessions from account capacity', () => fixture(async (r, f, sql) => {
    const active = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-zai'").one()).active;
    expect(active()).toBe(0);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,last_activity_at) VALUES ('hetzner','idle-old','idle-old','zcodex','running',?,'tasks','shared-zai',?)", at(), at(-10 * 3600000));
    expect(active()).toBe(0);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,last_activity_at) VALUES ('hetzner','idle-new','idle-new','zcodex','running',?,'tasks','shared-zai',?)", at(), at(-60000));
    expect(active()).toBe(1);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,last_activity_at) VALUES ('hetzner','idle-null','idle-null','zcodex','running',?,'tasks','shared-zai',NULL)", at());
    expect(active()).toBe(2);
  }));
});

describe('atomic final reports', () => {
  it('does not let a delayed progress report overwrite a terminal result', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", job.launch_id);
    const path = '/api/tasks/' + job.id + '/result';
    let release!: () => void;
    const stream = new ReadableStream<Uint8Array>({ start(controller) { release = () => { controller.enqueue(new TextEncoder().encode(JSON.stringify({status:'waiting-on-human',report:'Late progress',artifacts:[]}))); controller.close(); }; } });
    const pending = r.handle(new Request('https://hub.test' + path, {method:'POST',body:stream}), host());
    expect((await r.handle(req(path, {status:'done',report:'Completed',artifacts:[],confirmed_exit:true}), host())).status).toBe(200);
    release(); expect((await pending).status).toBe(409);
    const final: any = await (await r.handle(req('/api/tasks/' + job.id), client())).json(); expect(final.status).toBe('done'); expect(final.report).toBe('Completed');
  }));
  it('requires the task report endpoint for routed terminal exits', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", job.launch_id);
    expect((await f.handle(req('/api/fleet/launches/' + job.launch_id + '/result', {state:'exited',confirmed_exit:true}), host())).status).toBe(409);
  }));
  it('rolls back reservation release if persisting the final report fails', () => fixture(async (r, f, sql) => {
    const job: any = await (await r.handle(req('/api/tasks', input()), client())).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", job.launch_id);
    sql.exec("CREATE TRIGGER fail_report BEFORE UPDATE ON routing_jobs BEGIN SELECT RAISE(ABORT, 'injected report failure'); END");
    await expect(r.handle(req('/api/tasks/' + job.id + '/result', {status:'done',report:'Completed',artifacts:[],confirmed_exit:true}), host())).rejects.toThrow();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?',job.launch_id).one().state).toBe('running');
    sql.exec('DROP TRIGGER fail_report');
  }));
});

describe('fair queue scanning and self-contained capability context', () => {
  function backlog(sql: any) {
    for (let i = 1; i <= 21; i++) {
      const id = '00000000-0000-0000-0000-' + i.toString(16).padStart(12, '0');
      sql.exec('INSERT INTO routing_jobs (id,actor,request_key,request_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', id, 'client-a', 'backlog-' + i, JSON.stringify({workspace:'tasks',tier:'daily',model:null,needs:['unavailable'],tasks:[{prompt:'Read only',approved_action:'Read only'}],preferred_runner:null,preferred_machine:null}), 'queued', at(-1000), at(-1000));
    }
  }
  it('does not put a new eligible job behind twenty unavailable jobs', () => fixture(async (r, f, sql) => {
    backlog(sql);
    const job: any = await (await r.handle(req('/api/tasks', input({needs:['browser:orin','phone-sms']})), client())).json();
    expect(job.machine).toBe('win35');
    const prompt = sql.exec('SELECT prompt FROM fleet_launches WHERE id = ?',job.launch_id).one().prompt;
    expect(prompt).toContain('Required capabilities and profiles: browser:orin, phone-sms');
  }));
  it('rotates the scan cursor so already-queued ready work cannot starve', () => fixture(async (r, f, sql) => {
    backlog(sql); const id = 'ffffffff-ffff-ffff-ffff-ffffffffffff';
    sql.exec('INSERT INTO routing_jobs (id,actor,request_key,request_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', id, 'client-a', 'ready', JSON.stringify({workspace:'tasks',tier:'daily',model:null,needs:[],tasks:[{prompt:'Read only',approved_action:'Read only'}],preferred_runner:null,preferred_machine:null}), 'queued', at(), at());
    await r.dispatch(); expect(sql.exec('SELECT launch_id FROM routing_jobs WHERE id = ?', id).one().launch_id).toBeNull();
    const restarted = new RoutingService(sql, f, () => now); restarted.init();
    for (let i = 0; i < 21; i++) await restarted.dispatch();
    expect(sql.exec('SELECT launch_id FROM routing_jobs WHERE id = ?', id).one().launch_id).toBeTruthy();
  }));
  it('wraps under new arrivals and shares maintenance through an eligible nested reserve', () => fixture(async (r,f,sql) => {
    const insert=(id:string,needs:string[])=>sql.exec('INSERT INTO routing_jobs(id,actor,request_key,request_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',id,'client-a',id,JSON.stringify({workspace:'tasks',tier:'daily',model:null,needs,tasks:[{prompt:'Read only',approved_action:'Read only'}],preferred_runner:null,preferred_machine:null}),'queued',at(),at());
    insert('early',['unavailable']); insert('blocked',['unavailable']);
    await r.dispatch();
    sql.exec("UPDATE routing_jobs SET request_json=json_set(request_json,'$.needs',json('[]')) WHERE id='early'");
    const restarted=new RoutingService(sql,f,()=>now); restarted.init();
    const reconcile=vi.spyOn(f.admission,'reconcileOffers');
    const reserve=vi.spyOn(f,'reserve');
    const drain=vi.spyOn(f,'plan');
    for(let i=0;i<4;i++) {
      insert('new-'+i,['unavailable']);
      f.withMaintenance(()=>{restarted.dispatchNow();f.drainCapacityQueue();});
    }
    expect(sql.exec("SELECT launch_id FROM routing_jobs WHERE id='early'").one().launch_id).toBeTruthy();
    expect(reserve).toHaveBeenCalledTimes(1);
    expect(reconcile).toHaveBeenCalledTimes(4);
    expect(drain.mock.calls.length).toBeLessThanOrEqual(4*5+3);
    reconcile.mockRestore(); reserve.mockRestore(); drain.mockRestore();
  }));
  it('rejects coerced tier/difficulty types and redacts prefixed secrets and non-HTTP auth URIs', () => fixture(async (r) => {
    for (const data of [input({tier:['daily']}),input({difficulty:['normal']})]) expect((await r.handle(req('/api/tasks', data),client())).status).toBe(400);
    const safe = redact('{"access_token":"short-access","clientSecret":"secret phrase","auth":"otpauth://totp/private?secret=ABC"}');
    expect(safe).not.toContain('short-access'); expect(safe).not.toContain('secret phrase'); expect(safe).not.toContain('otpauth:');
  }));
});
