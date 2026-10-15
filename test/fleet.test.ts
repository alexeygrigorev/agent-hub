import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

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
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
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
  it('authenticates HTTP endpoints and denies an unauthenticated caller', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hub'));
    expect((await stub.fetch(request('', undefined, 'GET'))).status).toBe(401);
  });
  it('rejects credential-shaped registry fields and non-admin writes without storing them', async () => fixture(async (s, sql) => {
    const data: any = registry(); data.accounts[0].token = 'forbidden';
    expect((await s.handle(request('/accounts', data, 'PUT'), admin)).status).toBe(400);
    expect((await s.handle(request('/accounts', registry(), 'PUT'), host('hetzner'))).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) n FROM fleet_accounts').one().n).toBe(0);
  }));
  it('defaults to headless and preserves machine pinning; interactive requires a machine', async () => fixture(async (s, sql) => {
    await configure(s);
    const plan: any = s.plan(intent({ machine: 'win35' }));
    expect(plan.request.mode).toBe('headless'); expect(plan.selected.machine).toBe('win35');
    expect(s.plan(intent({ mode: 'interactive' }))).toEqual({ error: 'invalid_launch_request' });
    expect((s.plan(intent({ mode: 'interactive', machine: 'win35', prompt: '' })) as any).selected.machine).toBe('win35');
    sql.exec("UPDATE machines SET drained = 1 WHERE id = 'win35'");
    expect((s.plan(intent({ machine: 'win35' })) as any).selected).toBeNull();
  }));
  it('chooses by measured load and treats an unknown load as ineligible', async () => fixture(async (s, sql) => {
    await configure(s);
    const hb = { mem_avail_mb: 32000, disk_free_gb: 80, load: [30], gate: { launch_allowed: true, at: iso() } };
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'", JSON.stringify(hb));
    expect((s.plan(intent()) as any).selected.machine).toBe('win35');
    delete (hb as any).load;
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'win35'", JSON.stringify(hb));
    expect((s.plan(intent({ machine: 'win35' })) as any).rejected[0].reason).toBe('load_unknown');
  }));
  it('rejects stale/offline hosts, missing capabilities, resource floors and stale gates', async () => fixture(async (s, sql) => {
    await configure(s);
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'win35'", iso(-180001));
    expect((s.plan(intent({ machine: 'win35' })) as any).selected).toBeNull();
    sql.exec("UPDATE machines SET capabilities = '{}' WHERE id = 'hetzner'");
    expect((s.plan(intent({ machine: 'hetzner' })) as any).rejected[0].reason).toBe('capability_unavailable');
    sql.exec("UPDATE machines SET last_checkin_at = ?, last_heartbeat = ? WHERE id = 'win35'", iso(), JSON.stringify({ mem_avail_mb: 11000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso() } }));
    expect((s.plan(intent({ machine: 'win35' })) as any).rejected[0].reason).toBe('resource_limit');
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
  it('reserves a shared account once across machines; separate desktop Codex retains independent capacity', async () => fixture(async (s) => {
    await configure(s, true);
    const results = await Promise.all(['hetzner', 'win35'].map(machine => s.handle(request('/launches', intent({ machine })), admin)));
    expect(results.map(r => r.status).sort()).toEqual([201, 409]);
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
  it('binds claims to authenticated host and requires fresh account admission; duplicate claims cannot run twice', async () => fixture(async s => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    const claim = { gate_allowed: true, observed_at: iso(), account: 'shared-codex', profile_supported: true };
    expect((await s.handle(request(path, claim), host('win35'))).status).toBe(404);
    expect((await s.handle(request(path, { ...claim, observed_at: iso(-30001) }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, claim), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(path, claim), host('hetzner'))).status).toBe(409);
  }));
  it('retains capacity on uncertain effects and requires native receipt/confirmed death to release', async () => fixture(async s => {
    await configure(s);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    expect((await s.handle(request(path, { state: 'running' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, { state: 'uncertain' }), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).status).toBe(409);
    expect((await s.handle(request(path, { state: 'exited' }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path, { state: 'exited', confirmed_exit: true }), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).status).toBe(201);
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
