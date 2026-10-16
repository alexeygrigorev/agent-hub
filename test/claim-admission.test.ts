import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import { BusService } from '../src/bus';
import { claimCoversScope, normalizeScope } from '../src/claim_admission';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { sha256 } from '../src/auth/crypto';

const now = Date.now();
const at = (delta = 0) => new Date(now + delta).toISOString();
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' as const }, scopes: ['admin'] };
const host = (id = 'hetzner') => ({ caller: { machineId: id, kind: 'host' as const, actor: 'machine:' + id }, scopes: ['host'] });
const holder = { caller: { actor: 'worker-1', kind: 'agent' as const, tokenId: 'cred-worker-1' }, scopes: ['fleet:launch', 'bus:send'] };
const other = { caller: { actor: 'worker-2', kind: 'agent' as const, tokenId: 'cred-worker-2' }, scopes: ['fleet:launch', 'bus:send'] };
const routingClient = (actor = 'client-a') => ({ caller: { actor, kind: 'agent' as const, tokenId: 'cred-' + actor }, scopes: ['routing:submit', 'bus:send'] });
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

async function fleetFixture(fn: (f: FleetService, sql: any, bus: BusService) => Promise<void>, stubOverride?: any) {
  const bindings = env as unknown as Env;
  const stub = stubOverride ?? bindings.HUB.get(bindings.HUB.idFromName('claims-fleet-' + crypto.randomUUID()));
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
    credentials(sql, ['worker-1', 'worker-2']);
    f.admission.replacePolicy([{ workspace: 'demo', project: 'projA', require_claim: false }]);
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
    credentials(sql, ['client-a', 'worker-1']);
    f.admission.replacePolicy([{ workspace: 'tasks', project: 'tasks', require_claim: false }]);
    await fn(r, f, sql, bus);
  });
}

function credentials(sql: any, actors: string[]) {
  for (const actor of actors) sql.exec('INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 'cred-' + actor, 'agent', 'test-hash-' + actor, 'family-' + actor, actor, actor, JSON.stringify(['bus:send', 'fleet:launch', 'routing:submit']), 'test', at(), at(86400000));
}

function intent(extra: any = {}) {
  return { workspace: 'demo', prompt: 'private test goal', approved_action: 'Edit the declared files for the requested task', idempotency_key: crypto.randomUUID(), ...extra };
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
    expect((await edit({ caller: { kind: 'agent' as const }, scopes: ['fleet:launch'] }, { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect((await edit(admin, { mode: 'edit', claim_id: claim.id, scopes: ['src/fleet.ts'] })).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(0);
  }));

  it('rejects released and expired claims and does not offer after release while queued', () => fleetFixture(async (f, sql, bus) => {
    const live = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000, now);
    bus.releaseClaim(holder.caller, live.id, now);
    expect(((await (await f.handle(req('/api/fleet/launches', intent({ access: { mode: 'edit', claim_id: live.id, scopes: ['src/a.ts'] } })), holder)).json()) as any).error).toBe('claim_inactive');
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
    const claim = bus.claim({ actor: 'client-a', kind: 'agent' as const }, 'tasks', 'src', 'client-a', 3600000, now);
    const queued: any = await (await r.handle(req('/api/tasks', task({ needs: ['missing-cap'], access: { mode: 'edit', claim_id: claim.id, project: 'tasks', scopes: ['src/a.ts'] } })), routingClient())).json();
    expect(queued.launch_id).toBeNull(); expect(queued.access).toBe('edit');
    bus.releaseClaim({ actor: 'client-a', kind: 'agent' as const }, claim.id, now);
    sql.exec("UPDATE routing_machines SET needs_json = '[\"missing-cap\"]' WHERE machine_id = 'hetzner'");
    await r.dispatch();
    const after: any = await (await r.handle(req('/api/tasks/' + queued.id), routingClient())).json();
    expect(after.launch_id).toBeNull();
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('rejects routing-only use of another holder claim and expires before offer', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ actor: 'worker-1', kind: 'agent' as const }, 'projA', 'src', 'worker-1', 3600000, now);
    const stolen = await r.handle(req('/api/tasks', task({ access: { mode: 'edit', claim_id: claim.id, scopes: ['src/a.ts'] } })), routingClient());
    expect(stolen.status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').one().n).toBe(0);
    const own = bus.claim({ actor: 'client-a', kind: 'agent' as const }, 'tasks', 'src', 'client-a', 3600000, now);
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

const editAccess = (id: string, scope = 'src/a.ts') => ({ mode: 'edit', claim_id: id, scopes: [scope] });
const nativeStart = { gate_allowed: true, observed_at: at(), account: 'shared-codex', profile_supported: true };

describe('retained authority and replacement claims', () => {
  it('requires configured resource mapping, a genuine credential and bus authority', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const input = intent({ access: editAccess(claim.id) });
    f.admission.replacePolicy([]);
    expect((await (await f.handle(req('/api/fleet/launches', input), holder)).json() as any).error).toBe('claim_workspace_unconfigured');
    f.admission.replacePolicy([{ workspace: 'demo', project: 'projA', require_claim: false }]);
    expect((await f.handle(req('/api/fleet/launches', input), { ...holder, caller: { ...holder.caller, tokenId: 'missing' } })).status).toBe(403);
    sql.exec("UPDATE credentials SET scopes = '[\"fleet:launch\"]' WHERE id = 'cred-worker-1'");
    expect((await f.handle(req('/api/fleet/launches', input), holder)).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('rejects missing or unknown identities and unsupported root authority', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const input = intent({ access: editAccess(claim.id) });
    const missing = { ...holder, caller: { ...holder.caller, actor: undefined } };
    expect((await (await f.handle(req('/api/fleet/launches', input), missing)).json() as any).error).toBe('claim_actor_ambiguous');
    const unknown = { ...holder, caller: { ...holder.caller, actor: 'unknown' } };
    expect((await f.handle(req('/api/fleet/launches', input), unknown)).status).toBe(403);
    sql.exec("UPDATE claims SET holder = 'role:root' WHERE id = ?", claim.id);
    expect((await f.handle(req('/api/fleet/launches', intent({ access: editAccess(claim.id) })), holder)).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('rejects body identity/secret overrides and requires an explicit immutable action', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const input = intent({ access: editAccess(claim.id) });
    for (const extra of [{ caller: holder.caller }, { edit_authority: {} }, { token: 'test-only' }]) {
      expect((await f.handle(req('/api/fleet/launches', { ...input, ...extra }), holder)).status).toBe(400);
    }
    expect((await f.handle(req('/api/fleet/launches', { ...input, access: { ...input.access, holder: 'worker-1' } }), holder)).status).toBe(400);
    expect((await f.handle(req('/api/fleet/launches', { ...input, approved_action: undefined }), holder)).status).toBe(400);
    expect((await f.handle(req('/api/fleet/launches', input), holder)).status).toBe(201);
    expect((await f.handle(req('/api/fleet/launches', { ...input, approved_action: 'Do something else' }), holder)).status).toBe(409);
    const stored = JSON.parse(sql.exec('SELECT request_json FROM fleet_launches').one().request_json);
    expect(stored.edit_authority.actor).toBe(holder.caller.actor);
    expect(stored.edit_authority.credential_id).toBe('cred-worker-1');
    expect(JSON.stringify(stored)).not.toContain('test-hash');
  }));

  for (const mutation of ['revoked', 'expired', 'scope_removed', 'holder_changed', 'project_changed', 'claim_scope_changed'] as const) {
    it(`holds native start after ${mutation} and preserves capacity`, () => fleetFixture(async (f, sql, bus) => {
      const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
      const launched: any = await (await f.handle(req('/api/fleet/launches', intent({ access: editAccess(claim.id), machine: 'hetzner' })), holder)).json();
      if (mutation === 'revoked') sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred-worker-1'", at());
      if (mutation === 'expired') sql.exec("UPDATE credentials SET expires_at = ? WHERE id = 'cred-worker-1'", at(-1));
      if (mutation === 'scope_removed') sql.exec("UPDATE credentials SET scopes = '[\"fleet:launch\"]' WHERE id = 'cred-worker-1'");
      if (mutation === 'holder_changed') sql.exec("UPDATE agents SET tag = 'successor' WHERE tag = 'worker-1'");
      if (mutation === 'project_changed') f.admission.replacePolicy([{ workspace: 'demo', project: 'projB', require_claim: true }]);
      if (mutation === 'claim_scope_changed') sql.exec("UPDATE claims SET scope = 'src/a.ts' WHERE id = ?", claim.id);
      expect((await f.handle(req('/api/fleet/launches/' + launched.id + '/claim', nativeStart), host())).status).toBeGreaterThanOrEqual(400);
      expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launched.id).one().state).toBe('offered');
      expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(1);
      const dashboard: any = await (await f.handle(req('/api/fleet'), admin)).json();
      expect(dashboard.launches[0].admission_hold).toBeTruthy();
      expect(JSON.stringify(dashboard)).not.toContain('cred-worker-1');
      expect((await (await f.handle(req('/api/fleet/offers'), host())).json() as any).offers).toHaveLength(0);
    }));
  }

  for (const oldState of ['offered', 'starting', 'running', 'uncertain']) {
    it(`blocks overlapping replacement authority while old work is ${oldState}, then permits after confirmed exit`, () => fleetFixture(async (f, sql, bus) => {
      sql.exec("UPDATE agents SET project = 'projA' WHERE tag = 'worker-2'");
      const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
      const original: any = await (await f.handle(req('/api/fleet/launches', intent({ access: editAccess(claim.id), machine: 'hetzner' })), holder)).json();
      sql.exec('UPDATE fleet_launches SET state = ? WHERE id = ?', oldState, original.id);
      sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', now - 1, claim.id);
      const replacement = bus.claim(other.caller, 'projA', 'src/a.ts', 'worker-2', 3600000);
      const next = intent({ access: editAccess(replacement.id), machine: 'win35' });
      const denied: any = await (await f.handle(req('/api/fleet/launches', next), other)).json();
      expect(denied.error).toBe('claim_conflict');
      expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(1);
      // A host-confirmed terminal result is the existing lifecycle release authority.
      if (oldState === 'offered') sql.exec("UPDATE fleet_launches SET state = 'uncertain' WHERE id = ?", original.id);
      expect((await f.handle(req('/api/fleet/launches/' + original.id + '/result', { state: 'exited', confirmed_exit: true }), host())).status).toBe(200);
      expect((await f.handle(req('/api/fleet/launches', next), other)).status).toBe(201);
    }));
  }

  it('blocks overlapping distinct same-holder claims and preserves idempotent replay after release', () => fleetFixture(async (f, sql, bus) => {
    const broad = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const narrow = bus.claim(holder.caller, 'projA', 'src/a.ts', 'worker-1', 3600000);
    const inputs = [intent({ access: editAccess(broad.id) }), intent({ access: editAccess(narrow.id) })];
    const results = await Promise.all(inputs.map(input => f.handle(req('/api/fleet/launches', input), holder)));
    expect(results.map(r => r.status).sort()).toEqual([201, 409]);
    const index = results.findIndex(r => r.status === 201);
    const first: any = await results[index].json();
    bus.releaseClaim(holder.caller, index === 0 ? broad.id : narrow.id);
    const replay: any = await (await f.handle(req('/api/fleet/launches', inputs[index]), holder)).json();
    expect(replay.id).toBe(first.id); expect(replay.replay).toBe(true);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));
});

describe('atomic editable routing placement', () => {
  for (const change of ['release', 'expiry', 'reassign', 'credential_revocation', 'policy_change']) {
    it(`does not reserve after queued ${change}`, () => routingFixture(async (r, f, sql, bus) => {
      const claim = bus.claim({ kind: 'agent', actor: 'client-a' }, 'tasks', 'src', 'client-a', 3600000);
      const input = task({ needs: ['later'], access: editAccess(claim.id) });
      const queued: any = await (await r.handle(req('/api/tasks', input), routingClient())).json();
      expect(queued.launch_id).toBeNull();
      const stored = JSON.parse(sql.exec('SELECT request_json FROM routing_jobs WHERE id = ?', queued.id).one().request_json);
      expect(stored.edit_authority.credential_id).toBe('cred-client-a');
      expect(stored.tasks[0].approved_action).toBe(input.approved_action);
      if (change === 'release') bus.releaseClaim({ kind: 'agent', actor: 'client-a' }, claim.id);
      if (change === 'expiry') sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', now - 1, claim.id);
      if (change === 'reassign') sql.exec("UPDATE claims SET holder = 'worker-1' WHERE id = ?", claim.id);
      if (change === 'credential_revocation') sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred-client-a'", at());
      if (change === 'policy_change') f.admission.replacePolicy([{ workspace: 'tasks', project: 'projA', require_claim: true }]);
      sql.exec("UPDATE routing_machines SET needs_json = '[\"later\"]'");
      await r.dispatch();
      expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
      const after: any = await (await r.handle(req('/api/tasks/' + queued.id), routingClient())).json();
      expect(after.launch_id).toBeNull(); expect(after.selection).toBeNull();
      expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(0);
    }));
  }

  it('rejects a routing-only token even when its actor owns the live claim', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ kind: 'agent', actor: 'client-a' }, 'tasks', 'src', 'client-a', 3600000);
    sql.exec("UPDATE credentials SET scopes = '[\"routing:submit\"]' WHERE id = 'cred-client-a'");
    expect((await r.handle(req('/api/tasks', task({ access: editAccess(claim.id) })), routingClient())).status).toBe(403);
    expect(sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').one().n).toBe(0);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
  }));

  it('binds immutable routing actions and claims, deduplicates and admits only one conflicting launch', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ kind: 'agent', actor: 'client-a' }, 'tasks', 'src', 'client-a', 3600000);
    const input = task({ access: editAccess(claim.id) });
    const responses = await Promise.all([r.handle(req('/api/tasks', input), routingClient()), r.handle(req('/api/tasks', task({ access: editAccess(claim.id) })), routingClient())]);
    const jobs: any[] = await Promise.all(responses.map(r => r.json()));
    expect(jobs.filter(job => job.launch_id)).toHaveLength(1);
    const replay: any = await (await r.handle(req('/api/tasks', input), routingClient())).json();
    expect(replay.id).toBe(jobs[0].id);
    for (const changed of [{ approved_action: 'Different action' }, { access: editAccess(claim.id, 'src/b.ts') }, { access: { mode: 'read' } }]) {
      expect((await r.handle(req('/api/tasks', { ...input, ...changed }), routingClient())).status).toBe(409);
    }
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
    const launch = JSON.parse(sql.exec('SELECT request_json FROM fleet_launches').one().request_json);
    expect(launch.edit_authority.permission).toBe('routing:submit');
    expect(JSON.parse(launch.approved_action)).toEqual([{ prompt: input.prompt, approved_action: input.approved_action }]);
  }));

  it('rolls back reservation, selection and link if the routing link write fails', () => routingFixture(async (r, f, sql, bus) => {
    const claim = bus.claim({ kind: 'agent', actor: 'client-a' }, 'tasks', 'src', 'client-a', 3600000);
    const queued: any = await (await r.handle(req('/api/tasks', task({ needs: ['later'], access: editAccess(claim.id) })), routingClient())).json();
    sql.exec("UPDATE routing_machines SET needs_json = '[\"later\"]'");
    sql.exec("CREATE TRIGGER fail_link BEFORE UPDATE OF launch_id ON routing_jobs BEGIN SELECT RAISE(ABORT, 'injected link failure'); END");
    await expect(r.dispatch()).rejects.toThrow('injected link failure');
    sql.exec('DROP TRIGGER fail_link');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    const stored = sql.exec('SELECT * FROM routing_jobs WHERE id = ?', queued.id).one();
    expect(stored.launch_id).toBeNull(); expect(stored.selection_json).toBeNull();
    expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(0);
    await r.dispatch();
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));
});


describe('claims admission through authenticated Hub HTTP routes', () => {
  it('uses the authenticated claim holder and canonical fleet/task endpoints without accepting impersonation', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('claims-http-' + crypto.randomUUID()));
    const token = 'test-only-claims-holder';
    const adminToken = 'test-only-claims-admin';
    await fleetFixture(async (_f, sql) => {
      sql.exec("UPDATE credentials SET token_hash = ? WHERE id = 'cred-worker-1'", await sha256(token));
      sql.exec('INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 'http-admin', 'human', await sha256(adminToken), 'http-admin', 'worker-1', 'worker-1', JSON.stringify(['admin', 'fleet:launch', 'bus:send', 'routing:submit']), 'test', at(), at(86400000));
    }, stub);
    const http = (path: string, data: any, bearer = token) => stub.fetch(new Request('https://hub.test' + path, { method: 'POST', headers: { Authorization: 'Bearer ' + bearer, 'Content-Type': 'application/json' }, body: JSON.stringify(data) }));
    const made = await http('/api/bus/claims', { project: 'projA', scope: 'src', holder: 'worker-1', ttl_ms: 3600000 });
    expect(made.status).toBe(200);
    const claim: any = await made.json();
    const input = intent({ access: editAccess(claim.id) });
    expect((await http('/api/fleet/launches', input, adminToken)).status).toBe(403);
    expect((await http('/api/fleet/launches', { ...input, caller: holder.caller })).status).toBe(400);
    const offer = await http('/api/fleet/launches', input);
    expect(offer.status).toBe(201);
    const launch: any = await offer.json();
    expect(launch.access).toBe('edit');
    expect((await http('/api/fleet/launches', input)).status).toBe(200);
    const queued = await http('/api/fleet/tasks', task({ workspace: 'demo', needs: ['unavailable'], access: editAccess(claim.id) }));
    expect(queued.status).toBe(201);
    const job: any = await queued.json(); expect(job.launch_id).toBeNull(); expect(job.access).toBe('edit');
    const released = await http('/api/bus/claims/' + claim.id + '/release', {});
    expect(released.status).toBe(200);
    expect((await http('/api/fleet/launches', intent({ access: editAccess(claim.id) }))).status).toBe(409);
    await runInDurableObject(stub, async (hub: Hub) => {
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
      const retained = JSON.parse(String(hub.sql.exec('SELECT request_json FROM fleet_launches').one().request_json));
      expect(retained.edit_authority.credential_id).toBe('cred-worker-1');
      expect(retained.edit_authority.claim.holder).toBe('worker-1');
      expect(hub.fleetService.admission.heldReason(hub.sql.exec('SELECT * FROM fleet_launches').one())).toBe('claim_inactive');
    });
  });

  it('denies a stored routing-only credential owning a claim on the actual HTTP boundary', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('claims-http-routing-only-' + crypto.randomUUID()));
    const token = 'test-only-routing-scoped';
    let claimId = '';
    await fleetFixture(async (_f, sql, bus) => {
      claimId = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000).id;
      sql.exec("UPDATE credentials SET token_hash = ?, scopes = '[\"routing:submit\"]' WHERE id = 'cred-worker-1'", await sha256(token));
    }, stub);
    const denied = await stub.fetch(new Request('https://hub.test/api/fleet/tasks', { method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify(task({ workspace: 'demo', access: editAccess(claimId) })) }));
    expect(denied.status).toBe(403);
    expect((await denied.json() as any).error).toBe('claim_authority_unsupported');
    await runInDurableObject(stub, async (hub: Hub) => {
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').one().n).toBe(0);
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    });
  });
});

describe('scope lifecycle adoption boundaries', () => {
  it('retains an expired/deleted claim scope while running and admits disjoint scopes', () => fleetFixture(async (f, sql, bus) => {
    sql.exec("UPDATE agents SET project = 'projA' WHERE tag = 'worker-2'");
    const initial = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const launched: any = await (await f.handle(req('/api/fleet/launches', intent({ access: editAccess(initial.id) })), holder)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running' WHERE id = ?", launched.id);
    sql.exec('DELETE FROM claims WHERE id = ?', initial.id);
    const next = bus.claim(other.caller, 'projA', 'src/a.ts', 'worker-2', 3600000);
    expect((await (await f.handle(req('/api/fleet/launches', intent({ access: editAccess(next.id) })), other)).json() as any).error).toBe('claim_conflict');
    const disjoint = bus.claim(other.caller, 'projA', 'docs', 'worker-2', 3600000);
    expect((await f.handle(req('/api/fleet/launches', intent({ access: editAccess(disjoint.id, 'docs/guide.md') })), other)).status).toBe(201);
    expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(2);
  }));

  it('holds old unbound edit effects rather than guessing their authority or disjointness', () => fleetFixture(async (f, sql, bus) => {
    const initial = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const launched: any = await (await f.handle(req('/api/fleet/launches', intent({ access: editAccess(initial.id) })), holder)).json();
    const retained = JSON.parse(sql.exec('SELECT request_json FROM fleet_launches').one().request_json);
    delete retained.edit_authority;
    sql.exec('UPDATE fleet_launches SET request_json = ? WHERE id = ?', JSON.stringify(retained), launched.id);
    const separate = bus.claim(holder.caller, 'projA', 'docs', 'worker-1', 3600000);
    expect((await f.handle(req('/api/fleet/launches', intent({ access: editAccess(separate.id, 'docs/guide.md') })), holder)).status).toBe(409);
    expect((await f.handle(req('/api/fleet/launches/' + launched.id + '/claim', nativeStart), host())).status).toBe(403);
    expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(1);
  }));

  it('checks the final reservation even if claims change immediately after a successful preview', () => fleetFixture(async (f, sql, bus) => {
    const claim = bus.claim(holder.caller, 'projA', 'src', 'worker-1', 3600000);
    const input = intent({ access: editAccess(claim.id) });
    expect((await f.handle(req('/api/fleet/plan', input), holder)).status).toBe(200);
    bus.releaseClaim(holder.caller, claim.id);
    expect((await f.handle(req('/api/fleet/launches', input), holder)).status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(0);
  }));
});

describe('compatible retries and offer scanning', () => {
  it('replays a pre-adoption unspecified launch without labeling it read-only', () => fleetFixture(async (f, sql) => {
    const input = intent();
    const first: any = await (await f.handle(req('/api/fleet/launches', input), admin)).json();
    const retained = JSON.parse(sql.exec('SELECT request_json FROM fleet_launches').one().request_json);
    delete retained.access;
    sql.exec('UPDATE fleet_launches SET request_json = ? WHERE id = ?', JSON.stringify(retained), first.id);
    const replay: any = await (await f.handle(req('/api/fleet/launches', input), admin)).json();
    expect(replay.id).toBe(first.id); expect(replay.replay).toBe(true); expect(replay.access).toBe('legacy_unspecified');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(1);
  }));

  it('does not let twenty held offers hide eligible work while their capacity stays reserved', () => fleetFixture(async (f, sql, bus) => {
    sql.exec('UPDATE fleet_accounts SET max_concurrency = 30');
    for (let i = 0; i < 20; i++) {
      const claim = bus.claim(holder.caller, 'projA', 'dir' + i, 'worker-1', 3600000);
      expect((await f.handle(req('/api/fleet/launches', intent({ machine: 'hetzner', access: editAccess(claim.id, 'dir' + i + '/file') })), holder)).status).toBe(201);
    }
    sql.exec('UPDATE claims SET expires_at = ?', now - 1);
    sql.exec('UPDATE fleet_launches SET created_at = ?', at(-1000));
    const valid: any = await (await f.handle(req('/api/fleet/launches', intent({ machine: 'hetzner', access: { mode: 'read' } })), holder)).json();
    const offered: any = await (await f.handle(req('/api/fleet/offers'), host())).json();
    expect(offered.offers.map((offer: any) => offer.id)).toEqual([valid.id]);
    expect(f.accountStatus(sql.exec('SELECT * FROM fleet_accounts').one()).active).toBe(21);
  }));
});
