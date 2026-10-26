import {freshCpu} from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, isCapacityOnlyHold } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { SESSION_CONTROL_SCHEMA, SessionControlService } from '../src/session_control';
import { tickTbm } from '../src/tbm-controller';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
async function fixture(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init(); const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', iso(), now, iso(), now, 'ok', null, JSON.stringify(Object.fromEntries(['codex', 'claude'].map(provider => [provider, { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } }]))));
    }
    await fn(service, sql);
  });
}
function registry(shared = false) {
  return { accounts: [
    { label: 'shared-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
    { label: 'desktop-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
  ], routes: [
    { machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null },
    { machine_id: 'win35', engine: 'codex', account: shared ? 'shared-codex' : 'desktop-codex', profile: null },
  ] };
}
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'private test goal', idempotency_key: crypto.randomUUID(), ...extra });
async function configure(s: FleetService, shared = false) {
  expect((await s.handle(request('/accounts', registry(shared), 'PUT'), admin)).status).toBe(200);
}

describe('generic accounts and launch placement', () => {
  it('changes real Fleet placement for banked deadlines and excludes only the depleted account', async () => fixture(async (service, sql) => {
    await configure(service);
    const quota = (pct: number, banked = false) => ({ status: 'ok', observed_at: iso(-1000),
      details: { limit_reached: false, ...(banked ? { banked_resets: [
        { expires_at: iso(3600000), available: true, label: 'Full reset' },
      ] } : {}) }, windows: { weekly: { present: true, percent_remaining: pct,
        reset_at: iso(86400000), rolling: false } } });
    for (const [machine, account] of [['hetzner', 'shared-codex'], ['win35', 'desktop-codex']]) {
      const response = await service.handle(request('/subscriptions', { subscriptions: [{
        service: 'codex', account, plan: 'pro', quota: quota(80),
      }] }, 'PUT'), host(machine));
      expect(response.status).toBe(200);
    }
    tickTbm(sql, now);
    const baseline = service.plan(intent());
    expect(baseline.selected?.account).toBe('shared-codex');
    await service.handle(request('/subscriptions', { subscriptions: [{ service: 'codex', account: 'desktop-codex',
      plan: 'pro', quota: quota(80, true) }] }, 'PUT'), host('win35'));
    tickTbm(sql, now + 1000);
    const urgent = service.plan(intent());
    expect(urgent.selected?.account).toBe('desktop-codex');
    expect(urgent.selected?.tbm.banked).toBeGreaterThan(1);
    expect(urgent.selected?.tbm.pacing).toBeGreaterThan(1);
    // One drained account must not deny the healthy account on the same provider.
    const depleted = await service.handle(request('/subscriptions', { subscriptions: [{ service: 'codex', account: 'desktop-codex',
      plan: 'pro', quota: { ...quota(3), observed_at: iso() } }] }, 'PUT'), host('win35'));
    expect(depleted.status).toBe(200);
    const low = service.plan(intent());
    expect(low.selected?.account).toBe('shared-codex');
    expect(low.candidates?.some((r: any) => r.account === 'desktop-codex')).toBe(false);
  }));
  it('keeps configured Claude, native ZAI and Grok visible and routes policy models under allowed absolute workspace roots', async () => fixture(async (s, sql) => {
    const entries = [['shared-claude', 'claude', 'claude'], ['shared-zai', 'zai', 'zcodex'], ['shared-grok', 'grok', 'grok']];
    const config = {
      accounts: entries.map(([label, provider]) => ({ label, provider, max_concurrency: 8, reserve_percent: 10, enabled: true })),
      routes: entries.map(([account, , engine]) => ({ machine_id: 'hetzner', engine, account, profile: null })),
    };
    expect((await s.handle(request('/accounts', config, 'PUT'), admin)).status).toBe(200);
    const roots = ['/srv/projects', '/data/agents/project/worktrees'];
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({ engines: ['claude', 'zcodex', 'grok'], fleet_modes: ['interactive', 'headless'], workspaces: roots, cpu_count: 8 }), 'hetzner');
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false, has_grok_code_access: true }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    await s.handle(request('/subscriptions', { subscriptions: entries.map(([account, service]) => ({ account, service, plan: 'pro', quota })) }, 'PUT'), host('hetzner'));
    const fleet = await (await s.handle(request('', undefined, 'GET'), admin)).json() as any;
    for (const [label] of entries) {
      expect(fleet.accounts.find((a: any) => a.label === label)).toMatchObject({ placeable_slots: 8, placement_reasons: [] });
    }
    for (const [engine, tier, expected] of [['opus', 'deep', 'claude'], ['glm-flash', 'daily', 'zcodex']]) {
      const plan = await (await s.handle(request('/plan', intent({ mode: 'headless', workspace: '/data/agents/project/worktrees/recovery', engine, tier })), admin)).json() as any;
      expect(plan.selected?.engine, JSON.stringify(plan.rejected)).toBe(expected);
      expect(plan.rejected).toEqual([]);
    }
    const grok = await (await s.handle(request('/plan', intent({ engine: 'grok', tier: 'daily' })), admin)).json() as any;
    expect(grok.selected).toBeNull();
    expect(grok.rejected).toContainEqual(expect.objectContaining({ engine: 'grok', reason: 'provider_disabled' }));
    // Prefix siblings must never gain access when a root is added.
    const outside = await (await s.handle(request('/plan', intent({ engine: 'glm-flash', tier: 'daily', workspace: '/data/agents/project/worktrees-other/recovery' })), admin)).json() as any;
    expect(outside.selected).toBeNull();
    expect(outside.rejected).toContainEqual(expect.objectContaining({ reason: 'capability_unavailable' }));
  }));

  it('retires only a missing-adapter hold after fresh configuration admission', async () => fixture(async (s, sql) => {
    sql.exec("INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('zcodex', 'hetzner', 'unavailable', 'admission_unconfigured', ?)", iso());
    const probe = { engine: 'zcodex', status: 'ok', reason: 'admission_configured' };
    expect((await s.handle(request('/probe', probe), host('hetzner'))).status).toBe(200);
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='zcodex'").one().status).toBe('ok');
    sql.exec("UPDATE fleet_engine_health SET status='unavailable', reason='engine_auth_expired'");
    await s.handle(request('/probe', probe), host('hetzner'));
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='zcodex'").one().status).toBe('unavailable');
  }));
  it('authenticates HTTP endpoints and denies an unauthenticated caller', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hub'));
    expect((await stub.fetch(request('', undefined, 'GET'))).status).toBe(401);
  });
  it('accepts the configured operator bearer for fleet and rejects mixed authority', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-operator-' + crypto.randomUUID()));
    const token = 'synthetic_operator_fleet_token_0123456789';
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
    const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    await runInDurableObject(stub, async (hub: Hub) => { hub.env.HUB_ADMIN_TOKEN_SHA256 = hash; });
    const call = (headers: Record<string, string>) => stub.fetch(new Request('https://hub.test/api/fleet', { headers }));
    expect((await call({ Authorization: 'Bearer ' + token })).status).toBe(200);
    expect((await call({ Authorization: 'Bearer invalid' })).status).toBe(401);
    expect((await call({ Authorization: 'Bearer ' + token, Cookie: 'hub_session=invalid' })).status).toBe(403);
  });
  it('routes named provider profiles within their tier without treating Z.ai as Astra', async () => fixture(async (s, sql) => {
    const data = registry();
    data.routes[0].profile = 'secondary' as any;
    await s.handle(request('/accounts', data, 'PUT'), admin);
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    await s.handle(request('/subscriptions', { subscriptions: [{ service: 'codex', account: 'shared-codex', plan: 'pro', quota }] }, 'PUT'), host('hetzner'));
    expect(s.plan(intent({ machine: 'hetzner', tier: 'deep' })).selected?.profile).toBe('secondary');
    sql.exec("UPDATE fleet_accounts SET provider='zai' WHERE label='shared-codex'");
    expect(s.plan(intent({ machine: 'hetzner', tier: 'deep' })).selected).toBeNull();
    expect(s.plan(intent({ machine: 'hetzner', tier: 'deep' })).rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'tier_unavailable' });
    // A centrally managed account profile replaces the adapter profile name.
    sql.exec("UPDATE fleet_routes SET profile='hub-shared-codex' WHERE machine_id='hetzner'");
    await s.handle(request('/subscriptions', { subscriptions: [{ service: 'zai', account: 'shared-codex', plan: 'pro', quota }] }, 'PUT'), host('hetzner'));
    for (const selector of [{}, { tier: 'daily' }, { engine: 'glm-flash' }]) {
      expect(s.plan(intent({ machine: 'hetzner', ...selector })).selected?.selection).toMatchObject({ id: 'glm-flash', model: 'glm-5.3-flash' });
    }
    expect(s.plan(intent({ machine: 'hetzner', tier: 'deep' })).selected).toBeNull();

  }));
  it('resolves Astra to live Codex accounts, preserving pins, tiers and replay', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'win35'", iso(-180001));
    const input = intent({ engine: 'astra', tier: 'deep', access: { mode: 'read' } });
    const plan = s.plan(input);
    expect(plan.selected).toMatchObject({ machine: 'hetzner', engine: 'codex', account: 'shared-codex' });
    expect(plan.request.engine).toBe('astra');
    expect(plan.selected.selection).toMatchObject({ id: 'astra', model: 'gpt-6-astra' });
    expect(s.plan({ ...input, tier: 'quick' }).selected).toBeNull();
    expect(s.plan({ ...input, machine: 'win35' }).selected).toBeNull();
    expect(s.plan({ ...input, account: 'desktop-codex' }).selected).toBeNull();
    const first = s.reserve(input, admin);
    expect(first.status).toBe(201);
    expect(first.value.engine).toBe('codex');
    expect(first.value.selection).toMatchObject({ id: 'astra', model: 'gpt-6-astra' });
    const offers: any = await (await s.handle(request('/offers', undefined, 'GET'), host('hetzner'))).json();
    expect(offers.offers.find((o: any) => o.id === first.value.id)?.selection).toMatchObject({ id: 'astra', model: 'gpt-6-astra' });
    expect(s.reserve(input, admin).value).toMatchObject({ id: first.value.id, replay: true });
    expect(s.reserve({ ...input, engine: 'codex' }, admin).status).toBe(409);
    expect(sql.exec('SELECT * FROM resource_ramps WHERE launch_id=?', first.value.id).toArray()).toHaveLength(0); // Listing offers is not a start.
    const claim = { gate_allowed: true, profile_supported: true, observed_at: iso(), account: 'shared-codex' };
    for (const model of [undefined, 'gpt-6.1-sol']) {
      const refused = await s.handle(request('/launches/' + first.value.id + '/claim', { ...claim, model }), host('hetzner'));
      expect(refused.status).toBe(409);
      expect(await refused.json()).toMatchObject({ error: 'model_unsupported' });
      expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', first.value.id).one().state).toBe('offered');
    }
    expect((await s.handle(request('/launches/' + first.value.id + '/claim', { ...claim, model: 'gpt-6-astra' }), host('hetzner'))).status).toBe(200);
    expect(sql.exec('SELECT started_at FROM resource_ramps WHERE launch_id=?', first.value.id).one().started_at).toBe(iso());
    sql.exec("UPDATE fleet_accounts SET provider='zai' WHERE label='shared-codex'");
    expect(s.plan(input).selected).toBeNull();
  }));
  it('persists quick-tier Luna and exposes the resolved model in session views', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch = s.reserve(intent({ machine: 'hetzner', tier: 'quick', access: { mode: 'read' } }), admin).value;
    expect(launch.selection).toMatchObject({ id: 'luna', model: 'gpt-6-luna' });
    const sessions = new SessionControlService(sql, cb => cb(), () => now);
    sessions.init();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at) VALUES ('quick-test', 'owner', 'quick-key', '{}', ?, 'hetzner', 'codex', 'headless', 'offered', ?, ?)", launch.id, iso(), iso());
    const view = (sessions as any).view(sql.exec("SELECT * FROM session_control_sessions WHERE id='quick-test'").one());
    expect(view).toMatchObject({ engine: 'codex', model_id: 'luna', model: 'gpt-6-luna' });
  }));
  it('resolves Argon through both policy engine alternatives with the correct account', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE fleet_accounts SET provider='gemini' WHERE label='shared-codex'");
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    for (const engine of ['antigravity', 'gemini']) {
      sql.exec("UPDATE fleet_routes SET engine=? WHERE machine_id='hetzner'", engine);
      sql.exec("UPDATE machines SET capabilities=? WHERE id='hetzner'", JSON.stringify({ engines: [engine], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      await s.handle(request('/subscriptions', { subscriptions: [{ service: 'gemini', account: 'shared-codex', plan: 'pro', quota }] }, 'PUT'), host('hetzner'));
      expect(s.plan(intent({ engine: 'argon' })).selected).toMatchObject({ machine: 'hetzner', engine, account: 'shared-codex', selection: { id: 'argon', model: 'gemini-4-argon-high' } });
      expect(s.plan(intent({ tier: 'deep', engine })).selected?.selection).toMatchObject({ id: 'argon', model: 'gemini-4-argon-high' });
    }
  }));
  it('refuses disabled providers like grok with policy guidance even without a live route', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const engine of ['grok']) {
      const result = s.reserve(intent({ engine, machine: 'hetzner', access: { mode: 'read' } }), admin);
      expect(result.status).toBe(409);
      expect(result.value.error).toBe('provider_disabled');
      expect(result.value.detail).toContain('register a fresh host route');
    }
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));
  it('keeps deleted-cwd and unverified terminal census capacity until positive exit', () => fixture(async (s, sql) => {
    await configure(s);
    const id = crypto.randomUUID();
    const row = { native_id: id, tag: 'deleted-live', workspace: '/demo', cwd: '/gone (deleted)',
      engine: 'codex', phase: 'running', agent_state: null, profile: null, account: 'shared-codex', created_at_ms: now };
    const report = (value: any) => s.handle(request('/sessions', {sessions:[value]}, 'PUT'), host('hetzner'));
    const active = () => s.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active;
    expect((await report(row)).status).toBe(200);
    expect(sql.exec('SELECT phase FROM fleet_sessions WHERE native_id=?',id).one().phase).toBe('running');
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',id).one().state).toBe('running');
    expect(active()).toBe(1);
    for (const proof of [undefined, false]) {
      expect((await report({...row,phase:'exited',confirmed_exit:proof})).status).toBe(200);
      expect(sql.exec('SELECT phase FROM fleet_sessions WHERE native_id=?',id).one().phase).toBe('uncertain');
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',id).one().state).toBe('uncertain');
      expect(active()).toBe(1);
    }
    expect((await report({...row,phase:'exited',confirmed_exit:'true'})).status).toBe(400);
    expect(active()).toBe(1);
    expect((await report({...row,cwd:'/gone',phase:'exited',confirmed_exit:true})).status).toBe(200);
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',id).one().state).toBe('stopped');
    expect(active()).toBe(0);
  }));
  it('refuses Opus and Claude when no live route exists', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const engine of ['opus', 'claude']) {
      const result = s.reserve(intent({ engine, machine: 'hetzner', access: { mode: 'read' } }), admin);
      expect(result.status).toBe(409);
      expect(result.value.error).toBe('no_eligible_route');
    }
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));
  it('treats a null tier from session control as an omitted tier', async () => fixture(async (s) => {
    await configure(s);
    expect(s.plan(intent({ tier: null })).selected).toBeTruthy();
  }));
  it.each(['headless', 'interactive'])('settles a matching terminal %s incarnation, preserving absent and reused natives', (mode) => fixture(async (s, sql) => {
    await configure(s);
    const launch = await (await s.handle(request('/launches', intent({ mode, machine: 'hetzner' })), admin)).json() as any;
    const nativeId = crypto.randomUUID();
    sql.exec("UPDATE fleet_launches SET state='running', native_id=? WHERE id=?", nativeId, launch.id);
    const binding = { native_id: nativeId, tag: 'hub-' + launch.id, workspace: '/demo', native_created_at_ms: now };
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      crypto.randomUUID(), 'test-admin', 'terminal-test', '{}', launch.id, 'hetzner', 'codex', mode, 'running', JSON.stringify(binding), iso(), iso());
    const row = { native_id: nativeId, tag: binding.tag, workspace: binding.workspace, engine: 'codex', phase: 'exited', confirmed_exit: true, agent_state: null, profile: null, account: 'shared-codex', created_at_ms: now + 1 };
    const report = (sessions: any[]) => s.handle(request('/sessions', { sessions }, 'PUT'), host('hetzner'));
    expect((await report([row])).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('running');
    expect((await report([])).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('running');
    expect((await report([{ ...row, phase: 'running', cwd: '/gone (deleted)', created_at_ms: now }])).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('running');
    // Catalog evidence must not bypass the routed task-result contract.
    sql.exec("INSERT INTO routing_jobs (id,actor,request_key,request_json,launch_id,state,created_at,updated_at) VALUES ('task','test-admin','task-key','{}',?,'running',?,?)", launch.id, iso(), iso());
    expect((await report([{ ...row, created_at_ms: now }])).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('running');
    sql.exec("DELETE FROM routing_jobs WHERE id = 'task'");
    expect((await report([{ ...row, created_at_ms: now }])).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('exited');
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE fleet_launch_id=?', launch.id).one().state).toBe('stopped');
  }));
  it('rejects credential-shaped registry fields and non-admin writes without storing them', async () => fixture(async (s, sql) => {
    const data: any = registry(); data.accounts[0].token = 'forbidden';
    expect((await s.handle(request('/accounts', data, 'PUT'), admin)).status).toBe(400);
    expect((await s.handle(request('/accounts', registry(), 'PUT'), host('hetzner'))).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_accounts').one().n).toBe(0);
  }));
  it('counts an in-flight native spawn once in both account and machine capacity', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE fleet_accounts SET max_concurrency = 2 WHERE label = 'shared-codex'");
    sql.exec("INSERT INTO routing_machines (machine_id,needs_json,max_agents,runners_json,observed_at) VALUES ('hetzner','[]',2,'[]',?)", iso());
    for (const mode of ['headless', 'interactive']) {
      sql.exec('DELETE FROM fleet_sessions'); sql.exec('DELETE FROM fleet_launches');
      const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner', mode })), admin)).json();
      sql.exec("UPDATE fleet_launches SET state = 'starting' WHERE id = ?", launch.id);
      const tag = (mode === 'headless' ? 'task-hub-' : 'hub-') + launch.id;
      sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at) VALUES ('hetzner','spawned',?,'codex','running',NULL,?,'demo',NULL,'shared-codex',?)", tag, iso(), iso());
      const active = () => s.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active;
      expect(active()).toBe(1);
      sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at) VALUES ('hetzner','ambiguous',?,'codex','running',NULL,?,'demo',NULL,'shared-codex',?)", tag, iso(), iso());
      expect(active()).toBe(3); // ambiguous tag reuse never collapses two processes
      sql.exec("DELETE FROM fleet_sessions WHERE native_id='ambiguous'");
      expect(s.plan(intent({ machine: 'hetzner' })).selected?.machine).toBe('hetzner');
      for (const update of ["tag='foreign'", "workspace='foreign'", "engine='claude'", "profile='foreign'"]) {
        sql.exec('UPDATE fleet_sessions SET ' + update);
        expect(active()).toBe(2);
        sql.exec("UPDATE fleet_sessions SET tag=?,workspace='demo',engine='codex',profile=NULL", tag);
      }
      sql.exec("UPDATE fleet_sessions SET account='desktop-codex'");
      expect(s.plan(intent({ machine: 'hetzner' })).selected).toBeNull();
      sql.exec("UPDATE fleet_sessions SET account='shared-codex'");
      sql.exec("UPDATE fleet_launches SET state='offered' WHERE id=?", launch.id);
      expect(active()).toBe(2); // no claim: a tag alone proves no reservation linkage
      sql.exec("UPDATE fleet_launches SET state='running',native_id='different' WHERE id=?", launch.id);
      expect(active()).toBe(2); // a known UUID never falls back to matching tags
    }
  }));
  it('defaults to headless and supports automatic interactive placement and machine pinning', async () => fixture(async (s, sql) => {
    await configure(s);
    const plan: any = s.plan(intent({ machine: 'win35' }));
    expect(plan.request.mode).toBe('headless'); expect(plan.selected.machine).toBe('win35');
    expect(s.plan(intent({ mode: 'interactive', prompt: '' })).selected).toMatchObject({ engine: 'codex' });
    expect((s.plan(intent({ mode: 'interactive', machine: 'win35', prompt: '' })) as any).selected.machine).toBe('win35');
    sql.exec("UPDATE machines SET drained = 1 WHERE id = 'win35'");
    expect((s.plan(intent({ machine: 'win35' })) as any).selected).toBeNull();
  }));
  it('chooses by measured CPU and keeps unknown CPU advisory', async () => fixture(async (s, sql) => {
    await configure(s);
    const hb = { mem_avail_mb: 32000, disk_free_gb: 80, load: [30], cpu_usage: freshCpu(iso(),8,7), gate: { launch_allowed: true, at: iso() } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'", JSON.stringify(hb));
    expect((s.plan(intent()) as any).selected.machine).toBe('win35');
    delete (hb as any).cpu_usage;
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'win35'", JSON.stringify(hb));
    expect((s.plan(intent({ machine: 'win35' })) as any).selected.machine).toBe('win35');
    expect(s.resources.cpuHeadroom('win35').telemetry_state).toBe('unknown');
  }));
  it('rejects stale/offline hosts, missing capabilities, resource floors and stale gates', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'win35'", iso(-180001));
    expect((s.plan(intent({ machine: 'win35' })) as any).selected).toBeNull();
    sql.exec("UPDATE machines SET capabilities = '{}' WHERE id = 'hetzner'");
    expect((s.plan(intent({ machine: 'hetzner' })) as any).rejected[0].reason).toBe('capability_unavailable');
    sql.exec("UPDATE machines SET last_checkin_at = ?, last_heartbeat = ? WHERE id = 'win35'", iso(), JSON.stringify({ mem_avail_mb: 2400, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }));
    expect((s.plan(intent({ machine: 'win35' })) as any).rejected[0].reason).toBe('launcher_memory_reserve');
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'win35'", JSON.stringify({ gate: { launch_allowed: true, at: iso(-300001) } }));
    expect((s.plan(intent({ machine: 'win35' })) as any).rejected[0].reason).toBe('admission_unknown');
  }));
  it('uses conservative shared quota, ignores absent windows, and fails closed on unknown/expired snapshots', async () => fixture(async (s, sql) => {
    await configure(s, true);
    const quota = (percent: number | null) => JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { week: { present: true, percent_remaining: percent, reset_at: iso(3600000) }, absent: { present: false, percent_remaining: null } } } });
    sql.exec("UPDATE host_quota_snapshots SET routes_json = ? WHERE host_id = 'win35'", quota(15));
    expect((s.plan(intent()) as any).selected).toBeNull();
    sql.exec("UPDATE host_quota_snapshots SET routes_json = ? WHERE host_id = 'win35'", quota(null));
    expect((s.plan(intent()) as any).rejected.every((r: any) => r.reason === 'account_unknown')).toBe(true);
    sql.exec('UPDATE host_quota_snapshots SET captured_at = ?', iso(-1200001));
    expect((s.plan(intent()) as any).selected).toBeNull();
  }));
  it('reserves a shared account once across machines; the second waiter queues instead of failing', async () => fixture(async (s) => {
    await configure(s, true);
    const results = await Promise.all(['hetzner', 'win35'].map(machine => s.handle(request('/launches', intent({ machine })), admin)));
    // One shared slot: the loser parks as a waitable queued intent (202),
    // never a blind-retry reject — capacity-only failures queue, not 409.
    expect(results.map(r => r.status).sort()).toEqual([201, 202]);
    const bodies = await Promise.all(results.map(r => r.json())) as any[];
    expect(bodies.map(b => b.state).sort()).toEqual(['offered', 'queued']);
  }));
  it('keeps the Win35 account separate from shared Codex', async () => fixture(async s => {
    await configure(s);
    for (const machine of ['hetzner', 'win35']) expect((await s.handle(request('/launches', intent({ machine })), admin)).status).toBe(201);
  }));
  it('replays the same intent without another reservation and rejects key conflicts', async () => fixture(async s => {
    await configure(s); const input = intent();
    const first: any = await (await s.handle(request('/launches', input), admin)).json();
    const second: any = await (await s.handle(request('/launches', input), admin)).json();
    expect(second.id).toBe(first.id); expect(second.replay).toBe(true);
    expect((await s.handle(request('/launches', { ...input, prompt: 'changed' }), admin)).status).toBe(409);
    expect((await s.handle(request('/accounts', registry(), 'PUT'), admin)).status).toBe(409);
  }));
  it('adds an engine route to an unchanged shared pool while preserving active state', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const before = sql.exec('SELECT * FROM fleet_subscriptions').toArray();
    const config = registry();
    config.routes.push({ ...config.routes[0], engine: 'antigravity' });
    const result = await s.handle(request('/accounts', config, 'PUT'), admin);
    expect(result.status).toBe(200);
    expect(await result.json()).toEqual({ status: 'configured', added_routes: 1 });
    expect(sql.exec('SELECT * FROM fleet_subscriptions').toArray()).toEqual(before);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('offered');
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_routes').one().n).toBe(config.routes.length);
    const machine: any = sql.exec("SELECT capabilities FROM machines WHERE id = 'hetzner'").one();
    const capabilities = JSON.parse(machine.capabilities); capabilities.engines.push('antigravity');
    sql.exec("UPDATE machines SET capabilities = ? WHERE id = 'hetzner'", JSON.stringify(capabilities));
    const plan: any = s.plan(intent({ machine: 'hetzner', engine: 'antigravity' }));
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual({ machine: 'hetzner', engine: 'antigravity', reason: 'account_capacity' });
  }));
  it('prunes only unused stale routes while retaining live reservations and subscriptions', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE machines SET drained = 1 WHERE id = 'win35'");
    const before = sql.exec('SELECT * FROM fleet_subscriptions').toArray();
    const config = registry();
    config.routes.splice(1, 1);
    const res = await s.handle(request('/accounts', config, 'PUT'), admin);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: 'configured', added_routes: 0, removed_routes: 1 });
    expect(sql.exec('SELECT * FROM fleet_subscriptions').toArray()).toEqual(before);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('offered');
    // Even a stale route retains authority while its exact launch is active.
    sql.exec("UPDATE machines SET drained = 1 WHERE id = 'hetzner'");
    config.routes = [];
    expect((await s.handle(request('/accounts', config, 'PUT'), admin)).status).toBe(409);
  }));
  it('refuses policy decreases or route removal even when another route is added during a launch', async () => fixture(async (s, sql) => {
    await configure(s);
    await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin);
    for (const change of ['remove', 'profile', 'provider', 'reserve', 'enabled']) {
      const config = registry();
      config.routes.push({ ...config.routes[0], engine: 'antigravity' });
      if (change === 'remove') config.routes.splice(0, 1);
      if (change === 'profile') config.routes[0].profile = 'changed' as any;
      if (change === 'provider') config.accounts[0].provider = 'zai' as any;
      if (change === 'reserve') config.accounts[0].reserve_percent += 1;
      if (change === 'enabled') config.accounts[0].enabled = false as any;
      expect((await s.handle(request('/accounts', config, 'PUT'), admin)).status).toBe(409);
    }
    // A decrease (2 -> 1) still holds the registry after a raise widened the pool.
    const raised = registry(); raised.accounts[0].max_concurrency = 2;
    expect((await s.handle(request('/accounts', raised, 'PUT'), admin)).status).toBe(200);
    expect(sql.exec('SELECT max_concurrency AS n FROM fleet_accounts WHERE label = ?', 'shared-codex').one().n).toBe(2);
    expect((await s.handle(request('/accounts', registry(), 'PUT'), admin)).status).toBe(409);
    expect(sql.exec('SELECT max_concurrency AS n FROM fleet_accounts WHERE label = ?', 'shared-codex').one().n).toBe(2);
  }));
  it('applies a max_concurrency raise under live launches without touching reservations', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const config = registry();
    config.accounts[0].max_concurrency += 1;
    const result = await s.handle(request('/accounts', config, 'PUT'), admin);
    expect(result.status).toBe(200);
    expect(sql.exec('SELECT max_concurrency AS n FROM fleet_accounts WHERE label = ?', 'shared-codex').one().n).toBe(2);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('offered');
  }));
  it('queues on live MemAvailable and ignores resident launcher cap sums', async () => fixture(async (s, sql) => {
    await configure(s);
    const hb = { mem_avail_mb: 2400, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
      launcher_resources: { active_mem_mb: 29000, observed_at: iso() } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'", JSON.stringify(hb));
    const input = intent({ machine: 'hetzner', mode: 'headless' });
    expect(s.plan(input).rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', engine: 'codex', reason: 'launcher_memory_reserve', memory: expect.objectContaining({ mem_available_mb: 2400, reserved_mb: 0, floor_mb: 2048 }) }));
    const parked = s.reserve(input, admin);
    expect(parked.status).toBe(202);
    expect(parked.value.state).toBe('queued');
    hb.mem_avail_mb = 32000; // resident memory is already represented in this live measurement
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'", JSON.stringify(hb));
    s.drainCapacityQueue();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', parked.value.id).one().state).toBe('offered');
  }));
  it('holds placement on CPU full pressure and admits heavy CPU after recovery', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE machines SET capabilities = ? WHERE id='hetzner'", JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
    const hb: any = { mem_avail_mb: 32000, disk_free_gb: 80, load: [5, 13, 4], cpu_usage: {...freshCpu(iso(),8,8),psi_full_avg10:6}, gate: { launch_allowed: true, at: iso() } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id='hetzner'", JSON.stringify(hb));
    const input = intent({ machine: 'hetzner', mode: 'headless' });
    expect(s.plan(input).rejected?.[0]?.reason).toBe('cpu_pressure');
    expect(isCapacityOnlyHold(s.plan(input).rejected)).toBe(true);
    hb.load = [30, 30, 30]; hb.cpu_usage = freshCpu(iso(),8,1);
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id='hetzner'", JSON.stringify(hb));
    expect(s.plan(input).rejected?.some((r: any) => r.reason === 'cpu_pressure')).toBe(false);
  }));
  it('fails closed on stale launcher telemetry and counts unstarted offers', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE machines SET capabilities = ? WHERE id='hetzner'", JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8, launcher_resources: true }));
    const hb = { mem_avail_mb: 3000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() }, launcher_resources: { active_mem_mb: 0, observed_at: iso(-180001) } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id='hetzner'", JSON.stringify(hb));
    const input = intent({ machine: 'hetzner', mode: 'headless' });
    expect(s.plan(input).rejected?.[0]?.reason).toBe('admission_unknown');
    hb.launcher_resources.observed_at = iso();
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id='hetzner'", JSON.stringify(hb));
    expect(s.reserve(input, admin).status).toBe(201);
    expect(s.plan(intent({ machine: 'hetzner', mode: 'headless' })).rejected?.[0]?.reason).toBe('launcher_memory_reserve');
  }));
  it('binds claims to authenticated host and requires fresh account admission; duplicate claims cannot run twice', async () => fixture(async s => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    const claim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true };
    expect((await s.handle(request(path, claim), host('win35'))).status).toBe(404);
    expect((await s.handle(request(path, { ...claim, observed_at: iso(-30001) }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, claim), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(path, claim), host('hetzner'))).status).toBe(409);
  }));
  it.each([false, true])('stores runtime failure evidence with legacy enrichment=%s', async legacy => fixture(async (s, sql) => {
    await configure(s);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    const receipt = { state: 'failed', native_id: 'native-runtime', launcher_task_id: 'hub-' + l.id, confirmed_exit: true };
    await s.handle(request(path, { state: 'running', native_id: receipt.native_id, launcher_task_id: receipt.launcher_task_id }), host('hetzner'));
    const diagnostic = { reason: 'engine_oom_killed', message: 'The running engine was killed by the kernel OOM killer.', error_type: 'RuntimeError', stage: 'runtime' };
    if (legacy) expect((await s.handle(request(path, receipt), host('hetzner'))).status).toBe(200);
    for (let i = 0; i < 2; i++) expect((await s.handle(request(path, { ...receipt, diagnostic }), host('hetzner'))).status).toBe(200);
    const row: any = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', l.id).one();
    expect(row.state).toBe('failed');
    expect(JSON.parse(row.launch_error_json)).toEqual(diagnostic);
    expect(row.exit_reason).toBe('engine_oom_killed');
    expect((await s.handle(request(path, { ...receipt, diagnostic, native_id: 'wrong' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, { ...receipt, diagnostic }), host('win35'))).status).toBe(404);
    expect((await s.handle(request(path, { ...receipt, diagnostic: { ...diagnostic, reason: 'engine_exit_code' } }), host('hetzner'))).status).toBe(409);
  }));
  it('retains capacity on uncertain effects and requires native receipt/confirmed death to release', async () => fixture(async (s, sql) => {
    await configure(s);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    expect((await s.handle(request(path, { state: 'running' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, { state: 'uncertain' }), host('hetzner'))).status).toBe(200);
    // Capacity held by the uncertain reservation queues the next intent (202),
    // never a blind-retry reject.
    const waiterInput = intent({ machine: 'hetzner' });
    const waiter: any = await (await s.handle(request('/launches', waiterInput), admin)).json();
    expect(waiter.state).toBe('queued');
    expect((await s.handle(request(path, { state: 'exited' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, { state: 'exited', confirmed_exit: true }), host('hetzner'))).status).toBe(200);
    // The terminal result frees the slot and auto-places the queued waiter;
    // a brand-new intent then queues behind it.
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', waiter.id).one().state).toBe('offered');
    expect((await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).status).toBe(202);
  }));
  it('ignores sessions on stale offline machines when computing shared account capacity', async () => fixture(async (s, sql) => {
    await configure(s, true);
    // Both machines share shared-codex; win35 goes stale (legacy hetzner).
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'win35'", iso(-3600000));
    sql.exec("INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      'win35', 'stale-native', 'stale-tag', 'codex', 'running', null, iso(), 'demo', null, 'shared-codex');
    // Stale win35 session must not block the live hetzner route.
    const plan: any = s.plan(intent({ machine: 'hetzner' }));
    expect(plan.selected).toBeTruthy();
    expect(plan.selected.machine).toBe('hetzner');
  }));
  it('expires stale uncertain reservations without killing live running capacity', async () => fixture(async (s, sql) => {
    await configure(s);
    const first: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + first.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    expect((await s.handle(request('/launches/' + first.id + '/result', { state: 'uncertain' }), host('hetzner'))).status).toBe(200);
    // Second launch queues while the uncertain reservation is fresh.
    const waiterInput = intent({ machine: 'hetzner' });
    expect((await s.handle(request('/launches', waiterInput), admin)).status).toBe(202);
    // A 3-day-old uncertain hold never confirmed a native start; it must not block forever.
    sql.exec("UPDATE fleet_launches SET updated_at = ? WHERE id = ?", iso(-3 * 86400000), first.id);
    const third = await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin);
    // The freed slot promotes the earlier waiter FIFO; the newcomer queues.
    expect(third.status).toBe(202);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE request_key = ?', waiterInput.idempotency_key).one().state).toBe('offered');
  }));
  for (const terminal of ['exited', 'failed'] as const) {
    it(`replays the identical ${terminal} headless receipt without effects and refuses mismatched receipts`, async () => fixture(async (s, sql) => {
      await configure(s);
      const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless', prompt: 'owned task' })), admin)).json();
      await s.handle(request('/launches/' + launch.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
      const path = '/launches/' + launch.id + '/result';
      const receipt = { state: terminal, native_id: 'owned-native', launcher_task_id: 'hub-' + launch.id, confirmed_exit: true };
      expect((await s.handle(request(path, { ...receipt, state: 'running' }), host('hetzner'))).status).toBe(200);
      expect((await s.handle(request(path, receipt), host('hetzner'))).status).toBe(200);
      sql.exec('UPDATE fleet_launches SET updated_at = ? WHERE id = ?', '2020-01-01T00:00:00.000Z', launch.id);
      const before = sql.exec('SELECT state, native_id, launcher_task_id, updated_at FROM fleet_launches WHERE id = ?', launch.id).one();
      const replay = await s.handle(request(path, receipt), host('hetzner'));
      expect(replay.status).toBe(200);
      expect(await replay.json()).toEqual({ id: launch.id, state: terminal, replay: true });
      for (const wrong of [
        { ...receipt, native_id: 'foreign-native' },
        { ...receipt, launcher_task_id: 'hub-other' },
        { ...receipt, confirmed_exit: false },
        { ...receipt, state: terminal === 'exited' ? 'failed' : 'exited' },
        { ...receipt, unexpected: true },
      ]) expect((await s.handle(request(path, wrong), host('hetzner'))).status).toBe(409);
      expect((await s.handle(request(path, receipt), host('win35'))).status).toBe(404);
      expect(sql.exec('SELECT state, native_id, launcher_task_id, updated_at FROM fleet_launches WHERE id = ?', launch.id).one()).toEqual(before);
    }));
  }
  it('closes only the matching Hub assignment after a confirmed terminal host result', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const insert = (id: string, launch: string, machine: string) => sql.exec('INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', id, 'owner', id, '{}', launch, machine, 'headless', 'offered', iso(), iso());
    insert('matching', l.id, 'hetzner'); insert('foreign-host', l.id, 'win35'); insert('other-launch', 'other', 'hetzner');
    const state = (id: string) => sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id).one().state;
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    expect((await s.handle(request(path, { state: 'failed' }), host('hetzner'))).status).toBe(409);
    expect(state('matching')).toBe('offered');
    expect((await s.handle(request(path, { state: 'uncertain' }), host('hetzner'))).status).toBe(200);
    expect(state('matching')).toBe('uncertain');
    expect(state('foreign-host')).toBe('offered'); expect(state('other-launch')).toBe('offered');
    expect((await s.handle(request(path, { state: 'failed', confirmed_exit: true }), host('hetzner'))).status).toBe(200);
    expect(state('matching')).toBe('failed'); expect(state('foreign-host')).toBe('offered'); expect(state('other-launch')).toBe('offered');
  }));
  it('replays a fenced no-start failure and retains its terminal reason', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id;
    const receipt = { state: 'failed', confirmed_exit: true, reason: 'launcher_not_started', launcher_task_id: 'hub-' + launch.id };
    expect((await s.handle(request(path + '/result', receipt), host('hetzner'))).status).toBe(409);
    await s.handle(request(path + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    expect((await s.handle(request(path + '/result', { ...receipt, launcher_task_id: 'hub-other' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path + '/result', receipt), host('hetzner'))).status).toBe(200);
    expect(await (await s.handle(request(path + '/result', receipt), host('hetzner'))).json()).toMatchObject({ replay: true });
    expect(sql.exec('SELECT state, exit_reason, verification_status FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'failed', exit_reason: 'launcher_not_started', verification_status: 'not_started' });
  }));
  it('persists the actual sanitized refusal and exposes it on the canonical session', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const sessionId = crypto.randomUUID();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES (?, 'test-admin', ?, '{}', ?, NULL, 'headless', 'queued', ?, ?)", sessionId, sessionId, launch.id, iso(), iso());
    const path = '/launches/' + launch.id;
    await s.handle(request(path + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const diagnostic = { reason: 'launcher_memory_reserve', message: 'Host memory headroom after launcher reservations is below 10 GiB.', error_type: 'ValueError', exit_code: 1, stage: 'dispatch_headless' };
    const receipt = { state: 'failed', confirmed_exit: true, reason: diagnostic.reason, diagnostic, launcher_task_id: 'hub-' + launch.id };
    expect((await s.handle(request(path + '/result', { ...receipt, diagnostic: { ...diagnostic, message: 'bad\nmessage' } }), host('hetzner'))).status).toBe(400);
    expect((await s.handle(request(path + '/result', receipt), host('win35'))).status).toBe(404);
    expect((await s.handle(request(path + '/result', receipt), host('hetzner'))).status).toBe(200);
    expect(JSON.parse(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id = ?', launch.id).one().launch_error_json)).toEqual(diagnostic);
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', sessionId).one().state).toBe('queued');
    expect(await (await s.handle(request(path + '/result', receipt), host('hetzner'))).json()).toMatchObject({ replay: true });
    const sessions = new SessionControlService(sql, cb => cb(), () => now);
    sessions.init();
    const viewed = (sessions as any).view(sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionId).one());
    expect(viewed).toMatchObject({ state: 'queued', reason: diagnostic.reason, rejected: [expect.objectContaining({ reason: diagnostic.reason, memory: expect.objectContaining({ floor_mb: 2048 }) })] });
    const retry = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', viewed.fleet_launch_id).one();
    expect(retry.id).not.toBe(launch.id);
    expect(retry.state).toBe('queued');
    expect(sql.exec("SELECT detail FROM liveness_alerts WHERE subject_kind='launch' AND subject=?", launch.id).one().detail).toContain("2048");
    s.drainCapacityQueue();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', retry.id).one().state).toBe('queued');
    const hb = { mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() }, launcher_resources: { active_mem_mb: 0, observed_at: iso(1) } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'", JSON.stringify(hb));
    s.drainCapacityQueue();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', retry.id).one().state).toBe('offered');
  }));
  it('closes an unassigned queued canonical attempt when launcher_not_started occurs', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('sess-nostart', 'owner', 'key-nostart', '{}', ?, NULL, 'headless', 'queued', ?, ?)", launch.id, iso(), iso());
    const path = '/launches/' + launch.id;
    await s.handle(request(path + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const receipt = { state: 'failed', confirmed_exit: true, reason: 'launcher_not_started', launcher_task_id: 'hub-' + launch.id };
    expect((await s.handle(request(path + '/result', receipt), host('hetzner'))).status).toBe(200);
    expect(sql.exec('SELECT state, exit_reason, verification_status FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'failed', exit_reason: 'launcher_not_started', verification_status: 'not_started' });
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'sess-nostart'").one().state).toBe('failed');
  }));
  it('retains capacity for running sessions whose worktree disappeared', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      'hetzner', 'deleted-native', 'deleted-tag', 'codex', 'running', null, iso(), '/home/alexey/git/agent-hub-wt-old (deleted)', null, 'shared-codex');
    const status: any = await (await s.handle(request('', undefined, 'GET'), admin)).json();
    const account = status.accounts.find((a: any) => a.label === 'shared-codex');
    expect(account.active).toBe(1);
    expect(account.available_slots).toBe(account.max_concurrency - 1);
  }));
  it.each(['workspace_unavailable', 'admission_unconfigured', 'admission_refused', 'dispatch_refused', 'goal_startup_unconfigured', 'dispatch_route_unconfigured', 'dispatch_internal_error', 'codex_quota_gate_quota_unavailable', 'codex_quota_gate_codex_reserve_15_percent_required', 'codex_quota_gate_stale_window_evidence', 'codex_quota_gate_quota_check_failed'])('rejects only unclaimed own-host offers with explicit %s', async reason => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('pending', 'owner', 'pending', '{}', ?, 'hetzner', 'headless', 'offered', ?, ?)", launch.id, iso(), iso());
    const path = '/launches/' + launch.id + '/reject';
    const receipt = { reason };
    expect((await s.handle(request(path, receipt), host('win35'))).status).toBe(404);
    expect((await s.handle(request(path, receipt), admin)).status).toBe(403);
    expect((await s.handle(request(path, { reason: 'arbitrary text' }), host('hetzner'))).status).toBe(400);
    expect((await s.handle(request(path, receipt), host('hetzner'))).status).toBe(200);
    expect(sql.exec('SELECT state, exit_reason, verification_status FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'failed', exit_reason: reason, verification_status: 'not_started' });
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'pending'").one().state).toBe('failed');
    const diagnostic = JSON.parse(String(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id = ?', launch.id).one().launch_error_json));
    expect(diagnostic).toMatchObject({ reason, stage: 'dispatch_route' });
    expect(diagnostic.message.length).toBeGreaterThan(10);
    const sessions = new SessionControlService(sql, cb => cb(), () => now); sessions.init();
    const shown: any = await (await sessions.handle(new Request('https://hub.test/api/sessions/' + launch.id), admin)).json();
    expect(shown).toMatchObject({ reason, message: diagnostic.message });
    expect((await s.handle(request(path, receipt), host('hetzner'))).status).toBe(409);
    // Recovery requires an explicit successful probe; quota alone never clears the hold.
    if (reason === 'admission_unconfigured') {
      expect(s.plan(intent({ machine: 'hetzner' })).selected).toBeNull();
      expect(s.plan(intent({ machine: 'hetzner' })).rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason });
      expect(s.plan(intent()).selected?.machine).toBe('win35');
      await s.handle(request('/probe', { engine: 'codex', machine_id: 'hetzner', status: 'ok' }), admin);
    }
    const claimed: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + claimed.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    expect((await s.handle(request('/launches/' + claimed.id + '/reject', receipt), host('hetzner'))).status).toBe(409);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', claimed.id).one().state).toBe('starting');
  }));
  it('round-trips a full host diagnostic and surfaces it on the session', async () => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const legacy: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('legacy-show', 'owner', 'legacy-show', '{}', ?, 'hetzner', 'headless', 'offered', ?, ?)", legacy.id, iso(), iso());
    expect((await s.handle(request('/launches/' + legacy.id + '/reject', { reason: 'goal_startup_unconfigured' }), host('hetzner'))).status).toBe(200);
    const fixed = JSON.parse(String(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id = ?', legacy.id).one().launch_error_json));
    expect(fixed).toMatchObject({ reason: 'goal_startup_unconfigured', stage: 'dispatch_route' });
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('full-show', 'owner', 'full-show', '{}', ?, 'hetzner', 'headless', 'offered', ?, ?)", launch.id, iso(), iso());
    const diagnostic = { reason: 'goal_startup_unconfigured', message: fixed.message, stage: 'dispatch_route', error_type: 'ValueError' };
    const refused: any = await (await s.handle(request('/launches/' + launch.id + '/reject', { reason: diagnostic.reason, diagnostic }), host('hetzner'))).json();
    expect(refused).toMatchObject({ id: launch.id, state: 'failed', reason: diagnostic.reason, diagnostic });
    expect(JSON.parse(String(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id = ?', launch.id).one().launch_error_json))).toEqual(diagnostic);
    const sessions = new SessionControlService(sql, cb => cb(), () => now); sessions.init();
    const shown: any = await (await sessions.handle(new Request('https://hub.test/api/sessions/' + launch.id), admin)).json();
    expect(shown).toMatchObject({ reason: diagnostic.reason, message: diagnostic.message, launch_error: diagnostic });
  }));
  it('offers goal starts only to host-advertised Codex-family routes', async () => fixture(async (s, sql) => {
    await configure(s);
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    await s.handle(request('/subscriptions', { subscriptions: [
      { service: 'codex', account: 'shared-codex', plan: 'pro', quota },
      { service: 'codex', account: 'desktop-codex', plan: 'pro', quota },
    ] }, 'PUT'), host('hetzner'));
    await s.handle(request('/subscriptions', { subscriptions: [{ service: 'codex', account: 'desktop-codex', plan: 'pro', quota }] }, 'PUT'), host('win35'));
    const goalCaps = (routes: any[]) => ({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8, fleet_goal_routes: routes });
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify(goalCaps([{ engine: 'codex', account: 'shared-codex', profile: null }])), 'hetzner');
    const goal = intent({ mode: 'interactive', with_goal: true, tier: 'deep' });
    const planned = s.plan(goal);
    expect(planned.selected?.machine, JSON.stringify(planned.rejected)).toBe('hetzner');
    expect(planned.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'goal_unsupported' });
    const listed: any = await (await s.handle(request('', undefined, 'GET'), admin)).json();
    const routes = listed.routes.filter((r: any) => r.engine === 'codex');
    expect(routes.find((r: any) => r.machine_id === 'hetzner')).toMatchObject({ supports_goal: true });
    expect(routes.find((r: any) => r.machine_id === 'win35')).toMatchObject({ supports_goal: false });
    // A host release that does not advertise goal routes serves no goal start.
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }), 'hetzner');
    const unsupported = s.plan(goal);
    expect(unsupported.selected).toBeNull();
    expect(unsupported.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'goal_unsupported' });
    expect(unsupported.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'goal_unsupported' });
  }));
  it.each([false, true])('keeps unsupported-model session alive with fallback or explicit hold (pinned=%s)', async pinned => fixture(async (s, sql) => {
    await configure(s);
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);
    const input = intent({ ...(pinned ? { machine: 'hetzner' } : {}), access: { mode: 'read' } });
    const launch: any = await (await s.handle(request('/launches', input), admin)).json();
    expect(launch.machine).toBe('hetzner');
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('model-retry', 'test-admin', 'model-retry', '{}', ?, 'hetzner', 'headless', 'offered', ?, ?)", launch.id, iso(), iso());
    const path = '/launches/' + launch.id + '/reject';
    expect((await s.handle(request(path, { reason: 'model_unsupported' }), host('win35'))).status).toBe(404);
    const refusal: any = await (await s.handle(request(path, { reason: 'model_unsupported' }), host('hetzner'))).json();
    expect(refusal.id).not.toBe(launch.id);
    expect(refusal.state).toBe(pinned ? 'queued' : 'offered');
    expect(refusal.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'model_unsupported' });
    expect(sql.exec('SELECT state, verification_status FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'failed', verification_status: 'not_started' });
    expect(sql.exec("SELECT state, fleet_launch_id FROM session_control_sessions WHERE id='model-retry'").one()).toMatchObject({ state: 'queued', fleet_launch_id: refusal.id });
    const retry = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', refusal.id).one();
    expect(retry.machine_id).toBe(pinned ? '' : 'win35');
    const replay = s.reserve(input, admin);
    expect(replay.value.id).toBe(refusal.id);
    expect(s.plan(intent({ machine: 'hetzner' })).rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'model_unsupported' });
    // Refusal is scoped: interactive and another model on the same account work.
    expect(s.plan(intent({ machine: 'hetzner', mode: 'interactive', prompt: '' })).selected).not.toBeNull();
    expect(s.plan(intent({ machine: 'hetzner', tier: 'quick' })).selected).not.toBeNull();
    expect((await s.handle(request(path, { reason: 'model_unsupported' }), host('hetzner'))).status).toBe(409);
    if (!pinned) {
      await s.handle(request('/launches/' + refusal.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'desktop-codex', profile_supported: true }), host('win35'));
      expect((await s.handle(request('/launches/' + refusal.id + '/reject', { reason: 'model_unsupported' }), host('win35'))).status).toBe(409);
    }
  }));
  it('excludes a host-advertised unsupported route only for its account and mode', async () => fixture(async (s, sql) => {
    await configure(s);
    const caps = JSON.parse(sql.exec("SELECT capabilities FROM machines WHERE id='hetzner'").one().capabilities);
    caps.fleet_route_rejections = [{ engine: 'codex', account: 'shared-codex', profile: null, mode: 'headless', reason: 'model_unsupported' }];
    sql.exec("UPDATE machines SET capabilities=? WHERE id='hetzner'", JSON.stringify(caps));
    expect(s.plan(intent()).selected?.machine).toBe('win35');
    expect(s.plan(intent()).rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'model_unsupported' });
    expect(s.plan(intent({ mode: 'interactive', prompt: '' })).selected?.machine).toBe('hetzner');
  }));
  it('accepts complete native catalogs above 32 KiB while retaining catalog bounds', async () => fixture(async (s, sql) => {
    const sessions = Array.from({ length: 100 }, (_, i) => ({
      native_id: 'native-' + i, tag: 'native-' + i, engine: 'codex', phase: 'running',
      agent_state: 'idle', workspace: '/demo/' + 'x'.repeat(300), cwd: '/demo/' + 'x'.repeat(300),
      profile: null, account: null, last_activity_at: iso(), created_at_ms: now - 1000,
    }));
    expect(JSON.stringify({ sessions }).length).toBeGreaterThan(32768);
    expect((await s.handle(request('/sessions', { sessions }, 'PUT'), host('hetzner'))).status).toBe(200);
    expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE machine_id = 'hetzner'").one().n).toBe(100);
    const tooMany = Array.from({ length: 501 }, (_, i) => ({ ...sessions[0], native_id: 'large-' + i }));
    expect((await s.handle(request('/sessions', { sessions: tooMany }, 'PUT'), host('hetzner'))).status).toBe(400);
    const oversized = new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: ' '.repeat(3 * 1024 * 1024 + 1) });
    expect((await s.handle(oversized, host('hetzner'))).status).toBe(400);
    expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE machine_id = 'hetzner'").one().n).toBe(100);
  }));
  it('limits dashboards to admins and does not return prompts or credentials', async () => fixture(async s => {
    await configure(s); await s.handle(request('/launches', intent()), admin);
    expect((await s.handle(request('', undefined, 'GET'), host('hetzner'))).status).toBe(403);
    const result = await s.handle(request('', undefined, 'GET'), admin);
    const json: any = await result.json();
    expect(json.accounts[0].label).toBeTruthy(); expect(json.launches[0].prompt).toBeUndefined();
    expect(result.headers.get('Cache-Control')).toBe('private, no-store');
  }));
  it('supports two accounts for one service on a machine without reusing its default quota for both', async () => fixture(async (s, sql) => {
    const config = registry();
    config.routes[1].machine_id = 'hetzner';
    config.routes[1].profile = 'secondary' as any;
    await s.handle(request('/accounts', config, 'PUT'), admin);
    expect((s.plan(intent({ machine: 'hetzner' })) as any).selected).toBeNull();
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    const reports = { subscriptions: [{ service: 'codex', account: 'desktop-codex', plan: 'pro', quota }] };
    expect((await s.handle(request('/subscriptions', reports, 'PUT'), host('hetzner'))).status).toBe(200);
    expect((s.plan(intent({ machine: 'hetzner' })) as any).selected.account).toBe('desktop-codex');
    quota.windows.weekly.present = false;
    expect((await s.handle(request('/subscriptions', reports, 'PUT'), host('hetzner'))).status).toBe(400);
    quota.windows.weekly.present = true;
    (reports.subscriptions[0] as any).token = 'forbidden';
    expect((await s.handle(request('/subscriptions', reports, 'PUT'), host('hetzner'))).status).toBe(400);
    delete (reports.subscriptions[0] as any).token;
    expect((await s.handle(request('/subscriptions', reports, 'PUT'), host('win35'))).status).toBe(400);
  }));
  it('writes only changed session rows and retains vanished sessions as inactive history', async () => fixture(async (s, sql) => {
    const base = { tag: 't', workspace: '/repo', profile: null, account: null, engine: 'codex', phase: 'running', agent_state: 'idle' };
    const put = async (sessions: any[]) => (await (await s.handle(request('/sessions', { sessions }, 'PUT'), host('win35'))).json() as any).written;
    expect(await put([{ ...base, native_id: 'a' }, { ...base, native_id: 'b', tag: 'u' }])).toBe(2);
    expect(await put([{ ...base, native_id: 'a' }, { ...base, native_id: 'b', tag: 'u' }])).toBe(0);
    expect(await put([{ ...base, native_id: 'a', agent_state: 'working' }])).toBe(2);
    expect(sql.exec("SELECT native_id, agent_state FROM fleet_sessions WHERE machine_id = 'win35' AND phase = 'running'").toArray()).toEqual([{ native_id: 'a', agent_state: 'working' }]);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE native_id = 'b'").one().phase).toBe('unobserved');
  }));
  it('records authenticated requester, parent, summary and native sessions without allowing host attribution spoofing', async () => fixture(async (s, sql) => {
    await configure(s);
    const root: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner', summary: 'Parent task' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", root.id);
    const child: any = await (await s.handle(request('/launches', intent({ machine: 'win35', parent_id: root.id, summary: 'Child task' })), admin)).json();
    expect(child.id).toBeTruthy();
    const sessions = { sessions: [{ native_id: 'native-id', tag: 'real-session', workspace: '/repo', profile: null, account: null, engine: 'codex', phase: 'running', agent_state: 'working' }] };
    expect((await s.handle(request('/sessions', sessions, 'PUT'), host('win35'))).status).toBe(200);
    const view: any = await (await s.handle(request('', undefined, 'GET'), admin)).json();
    const record = view.launches.find((a: any) => a.id === child.id);
    expect(record.parent_id).toBe(root.id); expect(record.actor).toBe('test-admin'); expect(record.requester_kind).toBe('human'); expect(record.summary).toBe('Child task');
    expect(view.sessions[0].machine_id).toBe('win35'); expect(view.sessions[0].workspace).toBe('/repo');
    expect((await s.handle(request('/sessions', { ...sessions, machine_id: 'hetzner' }, 'PUT'), host('win35'))).status).toBe(400);
  }));
  it('requires a fresh account observation on the selected machine while keeping the other fresh machine usable', async () => fixture(async (s, sql) => {
    await configure(s, true);
    sql.exec("UPDATE host_quota_snapshots SET captured_at = ? WHERE host_id = 'win35'", iso(-1200001));
    expect((s.plan(intent({ machine: 'win35' })) as any).selected).toBeNull();
    expect((s.plan(intent({ machine: 'hetzner' })) as any).selected.machine).toBe('hetzner');
  }));
  it('does not attribute default quota to a named profile and clears reports on provider remapping', async () => fixture(async (s, sql) => {
    const config = registry(); config.routes[0].profile = 'secondary' as any;
    await s.handle(request('/accounts', config, 'PUT'), admin);
    expect((s.plan(intent({ machine: 'hetzner' })) as any).selected).toBeNull();
    sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', 'hetzner', 'shared-codex', 'codex', 'pro', iso(), '{}');
    config.accounts[0].provider = 'claude'; config.accounts[0].reserve_percent = 0;
    await s.handle(request('/accounts', config, 'PUT'), admin);
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_subscriptions').one().n).toBe(0);
  }));
  it('rejects unowned and inactive parent provenance and replays a child even after parent completion', async () => fixture(async (s, sql) => {
    await configure(s);
    const root: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const input = intent({ machine: 'win35', parent_id: root.id });
    expect((await s.handle(request('/launches', input), admin)).status).toBe(409);
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", root.id);
    const foreign = { caller: { actor: 'other-agent', kind: 'agent' }, scopes: ['fleet:launch'] };
    expect((await s.handle(request('/launches', input), foreign)).status).toBe(403);
    expect((await s.handle(request('/launches', input), admin)).status).toBe(201);
    sql.exec("UPDATE fleet_launches SET state = 'exited' WHERE id = ?", root.id);
    const replay: any = await (await s.handle(request('/launches', input), admin)).json();
    expect(replay.replay).toBe(true);
    const staleJob = { caller: { actor: 'job-owner', kind: 'job' }, scopes: ['jobs:create'] };
    expect((await s.handle(request('/launches', intent()), staleJob)).status).toBe(403);
  }));
});

describe('absolute workspace identifiers alongside labels', () => {
  const ABS = '/srv/hub/demo';
  function advertise(sql: any, workspaces: string[]) {
    const caps = { engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces, cpu_count: 8 };
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify(caps), 'hetzner');
  }
  it('routes an explicitly advertised absolute path and keeps labels working', async () => fixture(async (s, sql) => {
    await configure(s);
    advertise(sql, ['demo', ABS]);
    const plan: any = s.plan(intent({ workspace: ABS }));
    expect(plan.selected?.machine).toBe('hetzner');
    const launch: any = await (await s.handle(request('/launches', intent({ workspace: ABS })), admin)).json();
    expect(launch.machine).toBe('hetzner');
    // Labels keep working (hetzner slot is now consumed, so demo routes to win35).
    expect((s.plan(intent({ workspace: 'demo' })) as any).selected?.machine).toBe('win35');
  }));
  it('rejects malformed absolute paths before any claim or effect', async () => fixture(async (s, sql) => {
    await configure(s);
    advertise(sql, ['demo', ABS]);
    for (const workspace of [
      '/srv/../escape', '/srv//double', '/srv/./dot', '/srv/hub/demo/', '/', '',
      'C:\\workspaces\\..\\escape', 'C:\\\\workspaces', 'C:\\workspaces\\',
      '\\\\.\\COM1', 'C:\\workspaces\\CON', 'C:\\workspaces\\trailing ', 'C:\\workspaces\\trailing.'
    ]) {
      expect(s.plan(intent({ workspace })), workspace).toEqual({ error: 'invalid_launch_request' });
    }
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_launches').one().n).toBe(0);
  }));
  it('routes an explicitly advertised Windows absolute path', async () => fixture(async (s, sql) => {
    await configure(s);
    const WIN_ABS = 'C:\\workspaces\\my repo';
    const caps = { engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo', WIN_ABS], cpu_count: 8 };
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify(caps), 'win35');
    const plan: any = s.plan(intent({ workspace: WIN_ABS }));
    expect(plan.selected?.machine).toBe('win35');
    const launch: any = await (await s.handle(request('/launches', intent({ workspace: WIN_ABS })), admin)).json();
    expect(launch.machine).toBe('win35');
  }));
  it('refuses unadvertised absolute paths with no eligible route', async () => fixture(async (s, sql) => {
    await configure(s);
    advertise(sql, ['demo']);
    const plan: any = s.plan(intent({ workspace: ABS }));
    expect(plan.selected).toBeNull();
    expect(plan.rejected[0].reason).toBe('capability_unavailable');
    const res = await s.handle(request('/launches', intent({ workspace: ABS })), admin);
    expect(res.status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_launches').one().n).toBe(0);
  }));
  it('accepts absolute paths in the claim policy alongside labels', async () => fixture(async (s) => {
    await configure(s);
    const policy = { workspaces: [
      { workspace: 'demo', project: null, require_claim: false },
      { workspace: ABS, project: null, require_claim: true },
    ] };
    expect((await s.handle(request('/claim-policy', policy, 'PUT'), admin)).status).toBe(200);
    const bad = { workspaces: [{ workspace: '/srv/../escape', project: null, require_claim: true }] };
    expect((await s.handle(request('/claim-policy', bad, 'PUT'), admin)).status).toBe(400);
  }));
});

describe('bare workspace labels resolve under advertised absolute roots', () => {
  const ROOT = '/home/alexey/git';
  const LABEL = 'dataops-knowledge';
  const RESOLVED = ROOT + '/' + LABEL;
  function advertiseRoot(sql: any, machine: string, workspaces: string[]) {
    const caps = { engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces, cpu_count: 8 };
    sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify(caps), machine);
  }
  it('treats a bare label under an advertised root as capable and stores the absolute path', async () => fixture(async (s, sql) => {
    await configure(s);
    advertiseRoot(sql, 'hetzner', [ROOT]);
    const plan: any = s.plan(intent({ workspace: LABEL }));
    expect(plan.selected?.machine).toBe('hetzner');
    expect(plan.request.workspace).toBe(RESOLVED);
    expect(plan.rejected.some((r: any) => r.machine === 'hetzner' && r.reason === 'capability_unavailable')).toBe(false);
    const launch: any = await (await s.handle(request('/launches', intent({ workspace: LABEL })), admin)).json();
    expect(launch.machine).toBe('hetzner');
    const row: any = sql.exec('SELECT workspace, request_json FROM fleet_launches WHERE id = ?', launch.id).one();
    expect(row.workspace).toBe(RESOLVED);
    expect(JSON.parse(row.request_json).workspace).toBe(RESOLVED);
  }));
  it('leaves absolute workspace requests unchanged', async () => fixture(async (s) => {
    await configure(s);
    const plan: any = s.plan(intent({ workspace: RESOLVED }));
    // Fixture machines advertise only the 'demo' label: the absolute path
    // stays exactly as requested and fails closed without an advertised root.
    expect(plan.request.workspace).toBe(RESOLVED);
    expect(plan.selected).toBeNull();
  }));
  it('resolves an absolute path under an advertised root without rewriting', async () => fixture(async (s, sql) => {
    await configure(s);
    advertiseRoot(sql, 'hetzner', [ROOT]);
    const plan: any = s.plan(intent({ workspace: RESOLVED }));
    expect(plan.selected?.machine).toBe('hetzner');
    expect(plan.request.workspace).toBe(RESOLVED);
  }));
  it('fails closed with capability_unavailable when no absolute root is advertised', async () => fixture(async (s, sql) => {
    await configure(s);
    const plan: any = s.plan(intent({ workspace: 'unknown-label' }));
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'capability_unavailable' });
    const res = await s.handle(request('/launches', intent({ workspace: 'unknown-label' })), admin);
    expect(res.status).toBe(409);
    const body: any = await res.json();
    expect(body.error).toBe('no_eligible_route');
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_launches').one().n).toBe(0);
  }));
  it('keeps an explicitly mapped label exact and never rewrites it', async () => fixture(async (s, sql) => {
    await configure(s);
    advertiseRoot(sql, 'hetzner', [LABEL, ROOT]);
    const plan: any = s.plan(intent({ workspace: LABEL }));
    expect(plan.selected?.machine).toBe('hetzner');
    expect(plan.request.workspace).toBe(LABEL);
  }));
  it('resolves labels under an advertised root with a trailing separator', async () => fixture(async (s, sql) => {
    await configure(s);
    // Operator map values only require absoluteness, so a trailing slash
    // can reach capabilities; it must never block label resolution.
    advertiseRoot(sql, 'hetzner', [ROOT + '/']);
    const plan: any = s.plan(intent({ workspace: LABEL }));
    expect(plan.selected?.machine).toBe('hetzner');
    expect(plan.request.workspace).toBe(RESOLVED);
  }));
  it('capacity-only queue still works with incidental slugs alongside label resolution', async () => fixture(async (s, sql) => {
    const shared = registry(true);
    expect((await s.handle(request('/accounts', shared, 'PUT'), admin)).status).toBe(200);
    advertiseRoot(sql, 'hetzner', [ROOT]);
    // Fill the single shared slot via the resolved label.
    const first: any = await (await s.handle(request('/launches', intent({ workspace: LABEL, machine: 'hetzner' })), admin)).json();
    expect(first.state).toBe('offered');
    // Drain win35 so it reports incidental machine_unavailable (checked
    // before workspace capability) beside hetzner's account_capacity.
    sql.exec('UPDATE machines SET drained = 1 WHERE id = ?', 'win35');
    const queued = await s.handle(request('/launches', intent({ workspace: LABEL, idempotency_key: 'label-capacity-queue' })), admin);
    expect(queued.status).toBe(202);
    const body: any = await queued.json();
    expect(body.state).toBe('queued');
    expect(body.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'account_capacity' });
    expect(body.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'machine_unavailable' });
    expect(body.rejected.some((r: any) => r.reason === 'capability_unavailable')).toBe(false);
  }));
});

describe('imported session lifecycle and non-resurrection in fleet catalog', () => {
  it('prevents late live-catalog snapshots from resurrecting stopped same-incarnation sessions while allowing new UUID tag reuse', async () => fixture(async (s, sql) => {
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(x => x.trim())) sql.exec(statement);

    const NATIVE_ID_1 = '2d7bc739-3162-4fb9-9c39-691a1b328726';
    const NATIVE_ID_2 = '55555555-5555-4555-8555-555555555555';
    const TAG = 'task-hub-role-head-recovery';
    const WS = '/home/alexey/git/agent-hub';
    const INCARNATION = 1792338485058;

    // 1. Initial live catalog report
    const live1 = {
      native_id: NATIVE_ID_1,
      tag: TAG,
      engine: 'antigravity',
      phase: 'running',
      agent_state: 'working',
      workspace: WS,
      cwd: WS,
      profile: null,
      account: null,
      last_activity_at: iso(),
      created_at_ms: INCARNATION,
    };
    const rep1 = await s.handle(request('/sessions', { sessions: [live1] }, 'PUT'), host('hetzner'));
    expect(rep1.status).toBe(200);

    // Verified present in fleet_sessions and session_control_sessions (state=running)
    const row1 = sql.exec('SELECT * FROM fleet_sessions WHERE native_id = ?', NATIVE_ID_1).toArray()[0];
    expect(row1).toBeDefined();
    expect(row1.phase).toBe('running');

    const sc1 = sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', NATIVE_ID_1).toArray()[0];
    expect(sc1).toBeDefined();
    expect(sc1.state).toBe('running');

    // 2. Canonical positive retirement stops the session
    sql.exec("UPDATE session_control_sessions SET state = 'stopped', updated_at = ? WHERE id = ?", iso(), NATIVE_ID_1);

    // 3. Late live-catalog snapshot arrives reporting the SAME session/incarnation still running
    const lateRep = await s.handle(request('/sessions', { sessions: [live1] }, 'PUT'), host('hetzner'));
    expect(lateRep.status).toBe(200);

    // Same incarnation CANNOT resurrect:
    // History remains inactive and canonical control state remains stopped.
    const fleetCheck = sql.exec('SELECT * FROM fleet_sessions WHERE native_id = ?', NATIVE_ID_1).toArray();
    expect(fleetCheck.length).toBe(1);
    expect(fleetCheck[0].phase).toBe('unobserved');

    const scCheck = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', NATIVE_ID_1).toArray()[0];
    expect(scCheck.state).toBe('stopped');

    // 4. New UUID tag reuse: new session with same tag but new UUID is accepted normally
    const live2 = {
      native_id: NATIVE_ID_2,
      tag: TAG,
      engine: 'antigravity',
      phase: 'running',
      agent_state: 'working',
      workspace: WS,
      cwd: WS,
      profile: null,
      account: null,
      last_activity_at: iso(),
      created_at_ms: INCARNATION + 100000,
    };
    const rep2 = await s.handle(request('/sessions', { sessions: [live2] }, 'PUT'), host('hetzner'));
    expect(rep2.status).toBe(200);

    const row2 = sql.exec('SELECT * FROM fleet_sessions WHERE native_id = ?', NATIVE_ID_2).toArray()[0];
    expect(row2).toBeDefined();
    expect(row2.phase).toBe('running');

    const sc2 = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', NATIVE_ID_2).toArray()[0];
    expect(sc2).toBeDefined();
    expect(sc2.state).toBe('running');
  }));
});
