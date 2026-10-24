import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { LivenessService } from '../src/liveness';
import { reconcileAccountCapacity, accountCapacityAlerts } from '../src/account_capacity';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { SESSION_CONTROL_SCHEMA, SessionControlService } from '../src/session_control';

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

describe('phantom account reservations', () => {
  it('probes missing, stale and ambiguous bindings without polling every fresh live native', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'probe-native' WHERE id = ?", launch.id);
    const candidates = async () => (await (await f.handle(request('/reconciliation', undefined, 'GET'), host('hetzner'))).json() as any).launches.map((r: any) => r.id);
    expect(await candidates()).toEqual([launch.id]);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','probe-native','task','codex','running',?,'/repo','shared-codex',?)", iso(), now);
    expect(await candidates()).toEqual([]);
    sql.exec("UPDATE fleet_sessions SET phase = 'starting' WHERE native_id = 'probe-native'");
    expect(await candidates()).toEqual([]);
    for (const phase of ['broken', 'unknown', 'unobserved', 'exited']) {
      sql.exec("UPDATE fleet_sessions SET phase = ? WHERE native_id = 'probe-native'", phase);
      expect(await candidates()).toEqual([launch.id]);
    }
    sql.exec("UPDATE fleet_sessions SET phase = 'running', observed_at = ? WHERE native_id = 'probe-native'", iso(-180001));
    expect(await candidates()).toEqual([launch.id]);
    const foreign: any = await (await f.handle(request('/reconciliation', undefined, 'GET'), host('win35'))).json();
    expect(foreign.launches).toEqual([]);
  }));

  it('alerts with account numbers after ten minutes of phantom capacity queueing', () => fixture(async (f, sql) => {
    await configure(f);
    const first: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const waiting: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'uncertain' WHERE id = ?", first.id);
    expect(accountCapacityAlerts(sql, now)).toEqual([]);
    sql.exec('UPDATE fleet_launches SET created_at = ? WHERE id = ?', iso(-600001), waiting.id);
    expect(accountCapacityAlerts(sql, now)).toEqual([{ kind: 'launch', id: 'account-capacity/' + waiting.id, state: 'stuck', detail: 'account_capacity queued >10m: account=shared-codex, reserved=1, observed_live=0, max_concurrency=1' }]);
  }));

  it('keeps simultaneous memory and account alerts independently visible and recoverable', () => fixture(async (f, sql) => {
    await configure(f);
    const first: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const waiting: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'uncertain' WHERE id = ?", first.id);
    sql.exec('UPDATE fleet_launches SET created_at = ? WHERE id = ?', iso(-600001), waiting.id);
    sql.exec('INSERT INTO fleet_memory_holds (launch_id,since,rejected_json) VALUES (?,?,?)', waiting.id, iso(-600001), JSON.stringify([{reason:'launcher_memory_reserve',memory:{reserved_mb:2000,floor_mb:10240}}]));
    const live = new LivenessService(sql, undefined, () => now); live.init();
    live.reconcileAlerts(live.evaluate().subjects);
    const forSubject = (subject: string) => {
      const alert = live.alerts().find((a: any) => a.subject_kind === 'launch' && a.subject === subject);
      if (!alert) throw new Error('Expected an alert for ' + subject);
      return alert;
    };
    expect(forSubject(waiting.id)).toMatchObject({state:'launcher_memory_reserve',resolved_at:null});
    expect(forSubject(waiting.id).detail).toContain('reserved_mb');
    expect(forSubject('account-capacity/' + waiting.id)).toMatchObject({state:'stuck',resolved_at:null});
    expect(forSubject('account-capacity/' + waiting.id).detail).toContain('max_concurrency=1');
    sql.exec("UPDATE fleet_launches SET state = 'exited' WHERE id = ?", first.id);
    live.reconcileAlerts(live.evaluate().subjects);
    expect(forSubject('account-capacity/' + waiting.id).resolved_at).not.toBeNull();
    expect(forSubject(waiting.id).resolved_at).toBeNull();
    sql.exec('DELETE FROM fleet_memory_holds WHERE launch_id = ?', waiting.id);
    live.reconcileAlerts(live.evaluate().subjects);
    expect(forSubject(waiting.id).resolved_at).not.toBeNull();
  }));

  it('permits the exact legacy admin-token owner to cancel, with no cross-owner bypass', () => fixture(async (_f, sql) => {
    const service = new SessionControlService(sql, cb => cb(), () => now); service.init();
    const id = crypto.randomUUID();
    const owner = 'human:admin-token:0123456789abcdef';
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,state,mode,created_at,updated_at) VALUES (?,?,'cancel-owner','{}','held','interactive',?,?)", id, owner, iso(), iso());
    const cancel = () => new Request('https://hub.test/api/sessions/' + id + '/cancel', { method: 'POST', body: JSON.stringify({ key: 'owned-cancel' }) });
    const other = { caller: { isAdmin: true, kind: 'human', actor: 'human:admin-token:fedcba9876543210' }, scopes: ['admin'] };
    expect((await service.handle(cancel(), other)).status).toBe(404);
    const auth = { caller: { isAdmin: true, kind: 'human', actor: owner }, scopes: ['admin'] };
    const result = await service.handle(cancel(), auth);
    expect(result.status).toBe(200);
    expect((await result.json() as any).state).toBe('cancelled');
    expect((await service.handle(cancel(), auth)).status).toBe(200);
  }));

  it('cancels an owned admin-token capacity waiter and atomically retires its Fleet row', () => fixture(async (f, sql) => {
    await configure(f);
    const auth = { caller: { isAdmin: true, kind: 'human', actor: 'human:admin-token:0123456789abcdef' }, scopes: ['admin'] };
    await f.handle(request('/launches', intent({ machine: 'hetzner' })), auth);
    const waiter: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), auth)).json();
    expect(waiter.state).toBe('queued');
    const control = new SessionControlService(sql, cb => cb(), () => now); control.init();
    const id = crypto.randomUUID();
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,state,mode,fleet_launch_id,created_at,updated_at) VALUES (?,?,'queued-cancel','{}','queued','interactive',?,?,?)", id, auth.caller.actor, waiter.id, iso(), iso());
    const res = await control.handle(new Request('https://hub.test/api/sessions/' + id + '/cancel', { method: 'POST', body: JSON.stringify({ key: 'cancel-waiter' }) }), auth);
    expect(res.status).toBe(200);
    expect((await res.json() as any).state).toBe('cancelled');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', waiter.id).one().state).toBe('cancelled');
  }));

  it('previews terminal repairs, retains history and drains queued starts', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'native-dead' WHERE id = ?", launch.id);
    const waiting: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(waiting.state).toBe('queued');
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','native-dead','task','codex','exited',?,'/repo','shared-codex',?)", iso(), now);
    const preview = reconcileAccountCapacity(sql, now, false);
    expect(preview).toEqual([{ id: launch.id, before: 'running', after: 'exited', reason: 'native_terminal' }]);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
    const result: any = await (await f.handle(request('/reconcile', { apply: true }), admin)).json();
    expect(result.changes).toEqual(preview);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('exited');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', waiting.id).one().state).toBe('offered');
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
  }));

  it('holds ambiguous failures and catalog absence until host no-start proof arrives', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'uncertain', launch_error_json = ? WHERE id = ?", JSON.stringify({ reason: 'launcher_command_failed', stage: 'dispatch_headless' }), launch.id);
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(1);
    const candidates: any = await (await f.handle(request('/reconciliation', undefined, 'GET'), host('hetzner'))).json();
    expect(candidates.launches.map((r: any) => r.id)).toEqual([launch.id]);
    expect((await f.handle(request('/reconciliation', undefined, 'GET'), host('win35'))).status).toBe(200);
    const proof = { state: 'failed', confirmed_exit: true, reason: 'launcher_not_started', launcher_task_id: 'hub-' + launch.id };
    expect((await f.handle(request('/launches/' + launch.id + '/result', proof), host('hetzner'))).status).toBe(200);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(0);
  }));

  it('keeps a live native reservation despite conflicting terminal control state', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'live-native' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','live-native','task','codex','running',?,'/repo','shared-codex',?)", iso(), now);
    const control = new SessionControlService(sql, cb => cb(), () => now); control.init();
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,state,mode,fleet_launch_id,created_at,updated_at) VALUES ('control','test-admin','test','{}','failed','headless',?,?,?)", launch.id, iso(), iso());
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
    sql.exec("UPDATE fleet_sessions SET phase = 'exited' WHERE native_id = 'live-native'");
    expect(reconcileAccountCapacity(sql, now)).toHaveLength(1);
  }));

  it('holds terminal control reservations until exact native proof, including missing catalogs', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'gone' WHERE id = ?", launch.id);
    const service = new SessionControlService(sql, cb => cb(), () => now); service.init();
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,state,mode,fleet_launch_id,created_at,updated_at) VALUES ('terminal','test-admin','terminal','{}','stopped','headless',?,?,?)", launch.id, iso(), iso());
    expect(reconcileAccountCapacity(sql, now, false)).toEqual([]);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active).toBe(1);
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'terminal'").one().state).toBe('stopped');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','gone','task','codex','unobserved',?,'/repo','shared-codex',?)", iso(), now);
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
    sql.exec("UPDATE fleet_sessions SET phase = 'exited' WHERE native_id = 'gone'");
    expect(reconcileAccountCapacity(sql, now)[0].reason).toBe('native_terminal');
  }));

  it('ignores stale progress on orphan workloads while preserving unknown activity holds', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,last_activity_at,workspace,account) VALUES ('hetzner','orphan','orphan','codex','running',?,?,'/repo','shared-codex')", iso(), iso(-7 * 3600000));
    const account = sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one();
    expect(f.accountStatus(account).active).toBe(0);
    sql.exec("UPDATE fleet_sessions SET observed_at = ? WHERE native_id = 'orphan'", iso(1000));
    expect(f.accountStatus(account).active).toBe(0);
    sql.exec("UPDATE fleet_sessions SET last_activity_at = NULL WHERE native_id = 'orphan'");
    expect(f.accountStatus(account).active).toBe(1);
  }));
});

