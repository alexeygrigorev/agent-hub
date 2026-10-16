import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import { BusService } from '../src/bus';
import { claimCoversScope, normalizeScope } from '../src/claim_admission';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const now = Date.now();
const at = (delta = 0) => new Date(now + delta).toISOString();
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id = 'hetzner') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const holder = { caller: { actor: 'worker-1', kind: 'agent' }, scopes: ['fleet:launch'] };
const other = { caller: { actor: 'worker-2', kind: 'agent' }, scopes: ['fleet:launch'] };
const routingClient = (actor = 'client-a') => ({ caller: { actor, kind: 'agent' }, scopes: ['routing:submit'] });
const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const quota = { status: 'ok', observed_at: at(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: at(86400000) } } };

describe('scope coverage helpers', () => {
  it('normalizes and treats claim paths as ancestors', () => {
    expect(normalizeScope('src/./fleet.ts')).toBe('src/fleet.ts');
    expect(normalizeScope('src/../secret')).toBeNull();
    expect(claimCoversScope('src', 'src/fleet.ts')).toBe(true);
    expect(claimCoversScope('src/fleet.ts', 'src')).toBe(false);
    expect(claimCoversScope('src/a', 'src/b')).toBe(false);
  });
});

async function fleetFixture(fn: (f: FleetService, sql: any, bus: BusService) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('claims-fleet-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    f.init();
    const sql = hub.sql;
    const bus = new BusService(sql, cb => state.storage.transactionSync(cb));
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: at() } }), 0, JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', at(), now, at(), now, 'ok', null, JSON.stringify({ codex: { status: 'ok', observed_at: at(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: at(86400000) } } } }));
    }
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-1', 'projA', 'worker', 'hetzner', ?)", at());
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-2', 'projB', 'worker', 'win35', ?)", at());
    expect((await f.handle(req('/api/fleet/accounts', { accounts: [{ label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }], routes: [{ machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null }, { machine_id: 'win35', engine: 'codex', account: 'shared-codex', profile: null }] }, 'PUT'), admin)).status).toBe(200);
    await fn(f, sql, bus);
  });
}

async function routingFixture(fn: (r: RoutingService, f: FleetService, sql: any, bus: BusService) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('claims-routing-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now); f.init();
    const r = f.routing;
    const sql = hub.sql;
    const bus = new BusService(sql, cb => state.storage.transactionSync(cb));
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: at() } }), JSON.stringify({ engines: ['zcodex'], fleet_modes: ['headless'], workspaces: ['tasks'], cpu_count: 8 }));
    }
    expect((await f.handle(req('/api/fleet/accounts', { accounts: [{ label: 'shared-zai', provider: 'zai', max_concurrency: 3, reserve_percent: 0, enabled: true }], routes: ['hetzner', 'win35'].map(machine_id => ({ machine_id, engine: 'zcodex', account: 'shared-zai', profile: null })) }, 'PUT'), admin)).status).toBe(200);
    for (const machine of ['hetzner', 'win35']) {
      expect((await f.handle(req('/api/fleet/subscriptions', { subscriptions: [{ service: 'zai', account: 'shared-zai', plan: null, quota }] }, 'PUT'), host(machine))).status).toBe(200);
      expect((await r.handle(req('/api/routing/capabilities', { needs: machine === 'win35' ? ['browser:orin', 'phone-sms'] : [], max_agents: 2, runners: [{ engine: 'zcodex', profile: null, account: 'shared-zai', model: 'glm-5.3-flash', effort: 'high' }] }, 'PUT'), host(machine))).status).toBe(200);
    }
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('client-a', 'tasks', 'worker', 'hetzner', ?)", at());
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-1', 'projA', 'worker', 'hetzner', ?)", at());
    await fn(r, f, sql, bus);
  });
}

function intent(extra: any = {}) {
  return { workspace: 'demo', prompt: 'private test goal', idempotency_key: crypto.randomUUID(), ...extra };
}
function task(extra: any = {}) {
  return { prompt: 'Check browser state', approved_action: 'Read state only', needs: [], workspace: 'tasks', idempotency_key: crypto.randomUUID(), ...extra };
}

describe('fleet edit admission', () => {
  it('admits the holder for a covering claim and binds claim identity into the launch', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    const launched: any = await (await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'edit', claim_id: claim.id, project: 'projA', scopes: ['src/fleet.ts'] } })), holder)).json();
    expect(launched.state).toBe('offered'); expect(launched.access).toBe('edit');
    const stored = JSON.parse(sql.exec('SELECT request_json FROM fleet_launches WHERE id = ?', launched.id).one().request_json);
    expect(stored.access).toEqual({ mode: 'edit', claim_id: claim.id, project: 'projA', scopes: ['src/fleet.ts'] });
    expect(stored.prompt).toBe('private test goal');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));

  it('rejects wrong holder, wrong project, uncovered scope and ambiguous actor with zero capacity', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src/fleet.ts', 'worker-1', 3600000, now);
    const edit = (auth: any, access: any) => f.handle(req('/api/fleet/launches', intent({ access })), auth);
    expect((await edit(other, { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect((await edit(holder, { mode: 'edit', claim_id: claim.id, project: 'projB', scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect((await edit(holder, { mode: 'edit', claim_id: claim.id, scopes: ['src'] })).status).toBe(403);
    expect((await edit({ caller: { kind: 'agent' }, scopes: ['fleet:launch'] }, { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect((await edit(admin, { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(0);
  }));

  it('rejects released and expired claims and does not offer after release while queued', () => fleetFixture(async (f, sql, bus) => {
    const live = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    bus.releaseClaim(holder.caller, live.id, now);
    expect((await (await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'edit', claim_id: live.id, scopes: ['src/a.ts'] } })), holder)).json()).error).toBe('claim_inactive');
    const expired = bus.claim(holder.caller, 'projA', 'docs', 'worker-1', 3600000, now);
    sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', now - 1, expired.id);
    expect((await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'edit', claim_id: expired.id, scopes: ['docs/a.md'] } })), holder)).status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('allows only one active editable launch per claim, including distinct same-holder retries', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    const access = { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] };
    const a = intent({ access }); const b = intent({ access });
    const results = await Promise.all([f.handle(req('/api/fleet/launches', a), holder), f.handle(req('/api/fleet/launches', b), holder)]);
    expect(results.map(r => r.status).sort()).toEqual([201, 409]);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
    const winner = results[0].status === 201 ? a : b;
    const first: any = await results.find(r => r.status === 201)!.json();
    const replay: any = await (await f.handle(req('/api/fleet/launches', winner), holder)).json();
    expect(replay.id).toBe(first.id); expect(replay.replay).toBe(true);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));

  it('conflicts when the bound edit action or scopes change under the same key', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    const key = crypto.randomUUID();
    const first = intent({ idempotency_key: key, access: { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] } });
    expect((await f.handle(req('/api/fleet/launches', first), holder)).status).toBe(201);
    expect((await f.handle(req('/api/fleet/launches', { ...first, access: { mode: 'edit', claim_id: claim.id, scopes: ['src/routing.ts'] } }), holder)).status).toBe(409);
    expect((await f.handle(req('/api/fleet/launches', { ...first, prompt: 'different approved action' }), holder)).status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));

  it('rolls back a failed reservation with zero capacity effects', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    sql.exec("CREATE TRIGGER fail_launch BEFORE INSERT ON fleet_launches BEGIN SELECT RAISE(ABORT, 'injected launch failure'); END");
    await expect(f.handle(req('/api/fleet/launches', intent({ access: { mode: 'edit', claim_id: claim.id, scopes: ['src/a.ts'] } })), holder)).rejects.toThrow();
    sql.exec('DROP TRIGGER fail_launch');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(0);
  }));

  it('revalidates at native start, holds running work after expiry, and does not free capacity', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    const offered: any = await (await f.handle(req('/api/fleet/launches', intent({ machine: 'hetzner', access: { mode: 'edit', claim_id: claim.id, scopes: ['src/a.ts'] } })), holder)).json();
    const start = { gate_allowed: true, observed_at: at(), account: 'shared-codex', profile_supported: true };
    sql.exec('UPDATE claims SET released_at = ? WHERE id = ?', now, claim.id);
    expect((await f.handle(req('/api/fleet/launches/' + offered.id + '/claim', start), host())).status).toBe(409);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', offered.id).one().state).toBe('offered');
    sql.exec('UPDATE claims SET released_at = NULL, expires_at = ? WHERE id = ?', now + 3600000, claim.id);
    expect((await f.handle(req('/api/fleet/launches/' + offered.id + '/claim', start), host())).status).toBe(200);
    expect((await f.handle(req('/api/fleet/launches/' + offered.id + '/result', { state: 'running', native_id: 'native-1', launcher_task_id: 'task1' }), host())).status).toBe(200);
    sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', now - 1, claim.id);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', offered.id).one().state).toBe('running');
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(1);
    expect((await f.handle(req('/api/fleet/launches/' + offered.id + '/result', { state: 'uncertain' }), host())).status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', offered.id).one().state).toBe('uncertain');
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(1);
  }));

  it('keeps explicit read and legacy unspecified distinct and does not infer authority from prompts', () => fleetFixture(async (f, sql) => {
    const read: any = await (await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'read' }, prompt: 'please write files' })), holder)).json();
    expect(read.access).toBe('read');
    const legacy: any = await (await f.handle(req('/api/fleet/launches', intent({ prompt: 'please edit all files' })), admin)).json();
    expect(legacy.access).toBe('legacy_unspecified');
    expect(JSON.parse(sql.exec('SELECT request_json FROM fleet_launches WHERE id = ?', legacy.id).one().request_json).access.mode).toBe('legacy_unspecified');
  }));

  it('lets admins require claims for a workspace instead of relabeling unspecified launches read-only', () => fleetFixture(async (f, sql) => {
    expect((await f.handle(req('/api/fleet/claim-policy', { workspaces: [{ workspace: 'demo', project: 'projA', require_claim: true }] }, 'PUT'), holder)).status).toBe(403);
    expect((await f.handle(req('/api/fleet/claim-policy', { workspaces: [{ workspace: 'demo', project: 'projA', require_claim: true }] }, 'PUT'), admin)).status).toBe(200);
    const denied = await f.handle(req('/api/fleet/launches', intent({ prompt: 'unclaimed mutable' })), admin);
    expect(denied.status).toBe(403);
    expect((await denied.json() as any).error).toBe('claim_required');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    const stillRead: any = await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'read' } })), holder);
    expect(stillRead.status).toBe(201);
    expect((await stillRead.json() as any).access).toBe('read');
  }));
});

describe('routing edit admission and unchanged legacy routing', () => {
  it('does not offer after claim release between enqueue and placement', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ actor: 'client-a', kind: 'agent' }, 'tasks', 'src', 'client-a', 3600000, now);
    const queued: any = await (await r.handle(req('/api/tasks', task({ needs: ['missing-cap'], access: { mode: 'edit', claim_id: claim.id, project: 'tasks', scopes: ['src/a.ts'] } })), routingClient())).json();
    expect(queued.launch_id).toBeNull(); expect(queued.access).toBe('edit');
    bus.releaseClaim({ actor: 'client-a', kind: 'agent' }, claim.id, now);
    sql.exec("UPDATE routing_machines SET needs_json = '[\"missing-cap\"]' WHERE machine_id = 'hetzner'");
    await r.dispatch();
    const after: any = await (await r.handle(req('/api/tasks/' + queued.id), routingClient())).json();
    expect(after.launch_id).toBeNull();
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('rejects routing-only use of another holder claim and expires before offer', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ actor: 'worker-1', kind: 'agent' }, 'projA', 'src', 'worker-1', 3600000, now);
    const stolen = await r.handle(req('/api/tasks', task({ access: { mode: 'edit', claim_id: claim.id, scopes: ['src/a.ts'] } })), routingClient());
    expect(stolen.status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').one().n).toBe(0);
    const own = bus.claim({ actor: 'client-a', kind: 'agent' }, 'tasks', 'src', 'client-a', 3600000, now);
    sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', now - 1, own.id);
    expect((await r.handle(req('/api/tasks', task({ access: { mode: 'edit', claim_id: own.id, scopes: ['src/a.ts'] } })), routingClient())).status).toBe(409);
  }));

  it('places explicit read and legacy tasks with honest labels and keeps account/fairness behavior', () => routingFixture(async (r, f, sql) => {
    const legacy: any = await (await r.handle(req('/api/tasks', task()), routingClient())).json();
    expect(legacy.machine).toBe('hetzner'); expect(legacy.access).toBe('legacy_unspecified'); expect(legacy.tier).toBe('daily');
    expect(JSON.stringify(legacy)).not.toContain('Read state only');
    expect(sql.exec('SELECT prompt FROM fleet_launches WHERE id = ?', legacy.launch_id).one().prompt).toContain('Approved action: Read state only');
    sql.exec("DELETE FROM fleet_launches"); sql.exec("DELETE FROM routing_jobs");
    sql.exec('UPDATE routing_machines SET max_agents = 1');
    const first: any = await (await r.handle(req('/api/tasks', task({ access: { mode: 'read' } })), routingClient())).json();
    const second: any = await (await r.handle(req('/api/tasks', task()), routingClient())).json();
    const third: any = await (await r.handle(req('/api/tasks', task()), routingClient())).json();
    expect(first.access).toBe('read'); expect(first.machine).toBe('hetzner');
    expect(second.access).toBe('legacy_unspecified'); expect(second.machine).toBe('win35');
    expect(third.launch_id).toBeNull();
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-zai'").one()).active).toBe(2);
  }));
});
