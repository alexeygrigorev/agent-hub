import {freshCpu} from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { LivenessService } from '../src/liveness';
import { reconcileAccountCapacity, reconcileVerifiedNoStarts, accountCapacityAlerts, quotaProbeStaleAlerts } from '../src/account_capacity';
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

describe('phantom account reservations', () => {
  it('clears a copied capacity refusal when an offer is claimed for a new attempt', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless', tier: 'quick', access: { mode: 'read' } })), admin)).json();
    const refusal = {
      reason: 'launcher_memory_reserve',
      message: 'Host memory headroom after launcher reservations is below the configured floor.',
      error_type: 'ValueError', stage: 'dispatch_headless', exit_code: 1,
    };
    sql.exec('UPDATE fleet_launches SET launch_error_json=? WHERE id=?', JSON.stringify(refusal), launch.id);
    const selection = JSON.parse(sql.exec('SELECT model_selection_json FROM fleet_launches WHERE id=?', launch.id).one().model_selection_json);
    const claimed = await f.handle(request('/launches/' + launch.id + '/claim', {
      gate_allowed: true, profile_supported: true, observed_at: iso(), account: 'shared-codex', model: selection.model,
    }), host('hetzner'));
    expect(claimed.status).toBe(200);
    expect(sql.exec('SELECT state,launch_error_json FROM fleet_launches WHERE id=?', launch.id).one()).toMatchObject({
      state: 'starting', launch_error_json: null,
    });
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
    expect(reconcileVerifiedNoStarts(sql, now)).toEqual([]);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('starting');
  }));

  it('settles exact unbound launcher refusals while retaining ambiguous and unknown starts', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("UPDATE fleet_accounts SET max_concurrency = 4 WHERE label = 'shared-codex'");
    const makeUncertain = async () => {
      const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless' })), admin)).json();
      return launch.id as string;
    };
    const refusal = {
      reason: 'launcher_memory_reserve',
      message: 'Host memory headroom after launcher reservations is below the configured floor.',
      error_type: 'ValueError', stage: 'dispatch_headless', exit_code: 1,
    };
    const neverBound = await makeUncertain();
    const boundButUnobserved = await makeUncertain();
    const matchingNative = await makeUncertain();
    const ambiguousFailure = await makeUncertain();
    const ambiguous = {
      reason: 'launcher_command_failed',
      message: 'The launcher command failed with exit code 1; unrecognized output withheld.',
      error_type: 'RuntimeError', stage: 'dispatch_headless', exit_code: 1,
    };
    // Install all candidate receipts together so the normal maintenance path
    // on the next launch cannot settle the first exact refusal mid-fixture.
    for (const id of [neverBound, boundButUnobserved, matchingNative]) {
      sql.exec("UPDATE fleet_launches SET state='uncertain', native_id=NULL, launcher_task_id=NULL, launch_error_json=? WHERE id=?",
        JSON.stringify(refusal), id);
    }
    sql.exec("UPDATE fleet_launches SET state='uncertain', native_id=NULL, launcher_task_id=NULL, launch_error_json=? WHERE id=?",
      JSON.stringify(ambiguous), ambiguousFailure);

    const control = new SessionControlService(sql, cb => cb(), () => now); control.init();
    const addControl = (id: string, bindingJson: string | null) => sql.exec(
      "INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      'control-' + id, 'test-admin', 'control-' + id, '{}', id, 'hetzner', 'codex', 'headless', 'uncertain', bindingJson, iso(), iso());
    addControl(neverBound, null);
    addControl(boundButUnobserved, JSON.stringify({ native_id: 'still-bound-native', native_created_at_ms: now }));
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','still-catalog-native','task-hub-' || ?,'codex','running',?,'demo','shared-codex',?)",
      matchingNative, iso(), now);

    const capacity = () => f.accountStatus(
      sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one(), f.queueCapacitySnapshot(now));
    // On current main, active concurrency derives from observed live
    // sessions (eca41f28): the launch whose managed tag has a live native row
    // counts once via that observed native, not as a launch reservation.
    expect(capacity()).toMatchObject({ active: 4, reserved: 3, observed_sessions: 1, available_slots: 0 });

    const expected = [{ id: neverBound, before: 'uncertain', after: 'failed', reason: 'launcher_memory_reserve' }];
    expect(reconcileVerifiedNoStarts(sql, now, false)).toEqual(expected);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', neverBound).one().state).toBe('uncertain');
    expect(reconcileVerifiedNoStarts(sql, now)).toEqual(expected);

    expect(capacity()).toMatchObject({ active: 3, reserved: 2, observed_sessions: 1, available_slots: 1 });
    expect(sql.exec('SELECT state,exit_reason,verification_status FROM fleet_launches WHERE id=?', neverBound).one()).toMatchObject({
      state: 'failed', exit_reason: 'launcher_memory_reserve', verification_status: 'not_started',
    });
    expect(sql.exec('SELECT state,binding_json FROM session_control_sessions WHERE fleet_launch_id=?', neverBound).one()).toMatchObject({
      state: 'failed', binding_json: null,
    });
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', boundButUnobserved).one().state).toBe('uncertain');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', matchingNative).one().state).toBe('uncertain');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', ambiguousFailure).one().state).toBe('uncertain');
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE fleet_launch_id=?', boundButUnobserved).one().state).toBe('uncertain');
  }));

  it('settles verified no-start phantoms on the host census path', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain', native_id=NULL, launcher_task_id=NULL, launch_error_json=? WHERE id=?",
      JSON.stringify({
        reason: 'launcher_memory_reserve',
        message: 'Host memory headroom after launcher reservations is below the configured floor.',
        error_type: 'ValueError', stage: 'dispatch_headless', exit_code: 1,
      }), launch.id);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('uncertain');
    expect((await f.handle(request('/sessions', { sessions: [] }, 'PUT'), host('hetzner'))).status).toBe(200);
    expect(sql.exec('SELECT state,exit_reason,verification_status FROM fleet_launches WHERE id=?', launch.id).one()).toMatchObject({
      state: 'failed', exit_reason: 'launcher_memory_reserve', verification_status: 'not_started',
    });
  }));

  it('preserves legacy duplicate cwd and direct native identity capacity rules', () => fixture(async(f,sql)=>{
    await configure(f);
    const launch:any=await (await f.handle(request('/launches',intent({machine:'hetzner',mode:'headless'})),admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain',request_json=? WHERE id=?",'{"cwd":".","cwd":"other"}',launch.id);
    sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account) VALUES('hetzner','legacy-native',?,'codex','running',?,'demo','shared-codex')",'task-hub-'+launch.id,iso());
    const active=()=>f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active;
    expect(active()).toBe(1); // Existing SQL correlation uses the first cwd.
    sql.exec('UPDATE fleet_launches SET request_json=? WHERE id=?','{"cwd":"other","cwd":"."}',launch.id);
    expect(active()).toBe(2);
    // Exact native IDs already prove correlation regardless of tag/cwd or
    // engine metadata; do not narrow that branch while splitting the joins.
    sql.exec("UPDATE fleet_launches SET native_id='legacy-native',engine='claude' WHERE id=?",launch.id);
    expect(active()).toBe(1);
    sql.exec("UPDATE fleet_launches SET state='stopped' WHERE id=?",launch.id);
    expect(active()).toBe(1); // Orphan now holds the slot instead of reservation.
  }));

  it('counts an unbound native in the requested relative cwd once without correlating foreign or ambiguous natives', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("UPDATE machines SET capabilities = json_set(capabilities, '$.workspaces', json('[\"/repo\"]')) WHERE id = 'hetzner'");
    sql.exec("UPDATE fleet_accounts SET max_concurrency = 2 WHERE label = 'shared-codex'");
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', workspace: '/repo', cwd: '.local/probe/wt', mode: 'headless' })), admin)).json();
    expect(launch.state).toBe('offered');
    sql.exec("UPDATE fleet_launches SET state = 'uncertain' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','cwd-native',?,'codex','running',?,'/repo/.local/probe/wt','shared-codex',?)", 'task-hub-' + launch.id, iso(), now);
    const status = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one());
    expect(status().active).toBe(1);
    expect(status().available_slots).toBe(1);
    // The cwd must match the requested directory exactly, not a suffix or
    // arbitrary descendant with the same task tag.
    sql.exec("UPDATE fleet_sessions SET workspace = '/foreign/.local/probe/wt' WHERE native_id = 'cwd-native'");
    expect(status().active).toBe(2);
    sql.exec("UPDATE fleet_sessions SET workspace = '/repo/.local/probe/wt', engine = 'claude' WHERE native_id = 'cwd-native'");
    expect(status().active).toBe(2);
    sql.exec("UPDATE fleet_sessions SET engine = 'codex' WHERE native_id = 'cwd-native'");
    sql.exec("UPDATE fleet_sessions SET workspace = '/repo/.local/probe/wt', profile = 'foreign' WHERE native_id = 'cwd-native'");
    expect(status().active).toBe(2);
    sql.exec("UPDATE fleet_sessions SET profile = NULL WHERE native_id = 'cwd-native'");
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at,cwd,created_at_ms) SELECT machine_id,'duplicate-native',tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at,cwd,created_at_ms FROM fleet_sessions WHERE native_id = 'cwd-native'");
    expect(status().active).toBe(3);
    sql.exec("DELETE FROM fleet_sessions WHERE native_id = 'duplicate-native'");
    const next: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', workspace: '/repo' })), admin)).json();
    expect(next.state).toBe('offered');
    expect(sql.exec('SELECT state, native_id FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'uncertain', native_id: null });
  }));

  it.each([undefined, null, '.'])('preserves root correlation for cwd %s and holds malformed legacy intent', (cwd) => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless', cwd })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'uncertain' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','root-native',?,'codex','running',?,'demo','shared-codex',?)", 'task-hub-' + launch.id, iso(), now);
    const status = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one());
    expect(status().active).toBe(1);
    sql.exec("UPDATE fleet_launches SET request_json = 'invalid-json' WHERE id = ?", launch.id);
    expect(status().active).toBe(2);
  }));

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
    sql.exec('INSERT INTO fleet_catalog_seen VALUES (?,?)', 'hetzner', iso());
    for (const phase of ['starting', 'running']) {
      sql.exec("UPDATE fleet_sessions SET phase=? WHERE native_id='probe-native'", phase);
      expect(await candidates()).toEqual([]);
    }
    for (const stamp of [iso(-180001), iso(60001)]) {
      sql.exec('UPDATE fleet_catalog_seen SET observed_at=?', stamp);
      expect(await candidates()).toEqual([launch.id]);
    }
    sql.exec('UPDATE fleet_catalog_seen SET observed_at=?', iso());
    for (const phase of ['broken', 'unknown', 'uncertain', 'unobserved', 'exited']) {
      sql.exec("UPDATE fleet_sessions SET phase=? WHERE native_id='probe-native'", phase);
      expect(await candidates()).toEqual([launch.id]);
    }
    sql.exec("UPDATE fleet_sessions SET phase='running',engine='claude' WHERE native_id='probe-native'");
    expect(await candidates()).toEqual([launch.id]);
    sql.exec("UPDATE fleet_sessions SET engine='codex',machine_id='win35' WHERE native_id='probe-native'");
    expect(await candidates()).toEqual([launch.id]);
    sql.exec("UPDATE fleet_sessions SET machine_id='hetzner' WHERE native_id='probe-native'");
    sql.exec('UPDATE fleet_launches SET native_id=NULL WHERE id=?', launch.id);
    expect(await candidates()).toEqual([launch.id]);
    sql.exec("UPDATE fleet_launches SET native_id='probe-native' WHERE id=?", launch.id);
    expect(await candidates()).toEqual([]);
    expect((await f.handle(request('/sessions', {sessions: []}, 'PUT'), host('hetzner'))).status).toBe(200);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE native_id='probe-native'").one().phase).toBe('unobserved');
    expect(await candidates()).toEqual([launch.id]);
    const foreign: any = await (await f.handle(request('/reconciliation', undefined, 'GET'), host('win35'))).json();
    expect(foreign.launches).toEqual([]);
  }));

  it('batches exact-machine bindings while preserving first-row and optional-store fallbacks', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({machine:'hetzner'})), admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain' WHERE id=?", launch.id);
    const binding = async () => (await (await f.handle(request('/reconciliation', undefined, 'GET'), host('hetzner'))).json() as any).launches[0].binding;
    expect(await binding()).toEqual({});
    for (const [id, machine, value] of [['null',null,'{}'],['foreign','win35','{}'],['first','hetzner','{"tag":"first"}'],['second','hetzner','{"tag":"second"}']]) {
      sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,mode,state,binding_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', id,'binding-test',id,'{}',launch.id,machine,'headless','uncertain',value,iso(),iso());
    }
    const legacy = sql.exec('SELECT binding_json FROM session_control_sessions WHERE fleet_launch_id=? AND machine_id=?',launch.id,'hetzner').toArray()[0].binding_json;
    expect(await binding()).toEqual(JSON.parse(legacy));
    sql.exec("UPDATE session_control_sessions SET binding_json='invalid' WHERE id='first'");
    expect(await binding()).toEqual({});
    sql.exec("UPDATE session_control_sessions SET binding_json=NULL WHERE id='first'");
    expect(await binding()).toEqual({});
    // Simulate a Fleet-only store without deleting any production state.
    const withoutControl = new Proxy(sql, {get(target,key) {
      if(key==='exec') return (query:string,...args:any[]) => query.includes("name='session_control_sessions'")
        ? {toArray:()=>[]} : target.exec(query,...args);
      return target[key];
    }});
    const standalone = new FleetService(withoutControl, cb=>cb(), ()=>now);
    const result:any=await (await standalone.handle(request('/reconciliation',undefined,'GET'),host('hetzner'))).json();
    expect(result.launches[0].binding).toBeNull();
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
    sql.exec('INSERT OR REPLACE INTO fleet_memory_holds (launch_id,since,rejected_json) VALUES (?,?,?)', waiting.id, iso(-600001), JSON.stringify([{reason:'launcher_memory_reserve',memory:{reserved_mb:2000,floor_mb:10240}}]));
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

  it.each([false,true])('preserves the first control binding with mixed machine aliases (reverse=%s)', reverse => fixture(async (f,sql) => {
    await configure(f);
    const launch:any=await (await f.handle(request('/launches',intent({machine:'hetzner'})),admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain',native_id='binding-native' WHERE id=?",launch.id);
    sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,created_at_ms) VALUES('hetzner','binding-native','task','codex','exited',?,'/repo',?)",iso(),now);
    const machines=reverse ? ['hetzner',null] : [null,'hetzner'];
    for(const [i,machine] of machines.entries()) {
      sql.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,state,mode,machine_id,fleet_launch_id,binding_json,created_at,updated_at) VALUES(?,'test-admin',?,'{}','running','headless',?,?,?, ?,?)",'binding-'+i,'binding-'+i,machine,launch.id,'{}',iso(),iso());
    }
    for(const matchedMachine of machines) {
      for(const [i,machine] of machines.entries()) {
        const proof={native_id:'binding-native',tag:machine===matchedMachine?'task':'foreign',workspace:'/repo',native_created_at_ms:now};
        sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify(proof),'binding-'+i);
      }
      const legacy=sql.exec('SELECT state,binding_json FROM session_control_sessions WHERE fleet_launch_id=? AND (machine_id IS NULL OR machine_id=?)',launch.id,'hetzner').toArray()[0];
      const expected=JSON.parse(legacy.binding_json).tag==='task';
      expect(reconcileAccountCapacity(sql,now,false)).toHaveLength(expected?1:0);
      expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',launch.id).one().state).toBe('uncertain');
    }
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

  it('reports a live-native/terminal-control lifecycle mismatch once under its real account', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("INSERT INTO fleet_accounts VALUES ('shared-claude', 'claude', 13, 15, 1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('hetzner', 'claude', 'shared-claude', NULL)");
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', engine: 'claude', account: 'shared-claude' })), admin)).json();
    expect(launch).toMatchObject({ state: 'offered', machine: 'hetzner', engine: 'claude', account: 'shared-claude' });
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'live-native' WHERE id = ?", launch.id);
    const report = (machine: string, sessions: any[]) => f.handle(request('/sessions', { sessions, census_complete: true }, 'PUT'), host(machine));
    expect((await report('hetzner', [{ native_id: 'live-native', tag: 'task', engine: 'claude', phase: 'running', agent_state: 'idle', workspace: 'demo', profile: null, account: 'shared-claude' }])).status).toBe(200);
    expect((await report('win35', [])).status).toBe(200);
    const oldNativeAt = now - 30 * 86400000;
    sql.exec('UPDATE fleet_launches SET created_at = ? WHERE id = ?', iso(-30 * 86400000), launch.id);
    sql.exec('UPDATE fleet_sessions SET created_at_ms = ? WHERE native_id = ?', oldNativeAt, 'live-native');
    const control = new SessionControlService(sql, cb => cb(), () => now); control.init();
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,state,mode,machine_id,engine,fleet_launch_id,binding_json,created_at,updated_at) VALUES ('control','test-admin','test','{}','stopped','headless','hetzner','claude',?,?,?,?)",
      launch.id, JSON.stringify({ native_id: 'live-native', tag: 'task', workspace: 'demo', native_created_at_ms: oldNativeAt }), iso(-30 * 86400000), iso(-30 * 86400000));

    const fleet = async () => await (await f.handle(new Request('https://hub.test/api/fleet'), admin)).json();
    const snapshot: any = await fleet();
    const account = snapshot.accounts.find((row: any) => row.label === 'shared-claude');
    expect(account).toMatchObject({ active: 1, reserved: 0, observed_sessions: 1, native_live: 1 }); // Derived from observed live session, no reservation double count.
    expect(snapshot.accounts.find((row: any) => row.label === 'shared-codex').active).toBe(0);
    expect(snapshot.sessions.find((row: any) => row.native_id === 'live-native')).toMatchObject({ phase: 'running', account: 'shared-claude' });
    expect(snapshot.launches.find((row: any) => row.id === launch.id)).toMatchObject({ state: 'running', hub_session_id: 'control' });
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'control'").one().state).toBe('stopped');
    expect(reconcileAccountCapacity(sql, now)).toEqual([]);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE native_id = 'live-native'").one().phase).toBe('running');
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'control'").one().state).toBe('stopped');
    sql.exec("UPDATE fleet_sessions SET phase = 'exited' WHERE native_id = 'live-native'");
    expect(reconcileAccountCapacity(sql, now)).toEqual([{ id: launch.id, before: 'running', after: 'exited', reason: 'native_terminal' }]);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('exited');
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = 'control'").one().state).toBe('stopped');
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

  it('frees physical capacity for an unobserved native only with a fresh catalog, preserving launch history', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'gone' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','gone','task','codex','unobserved',?,'/repo','shared-codex',?)", iso(), now);

    const active = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active;
    expect(active()).toBe(1); // Missing or stale catalog evidence keeps the reservation.

    sql.exec('INSERT INTO fleet_catalog_seen VALUES (\'hetzner\', ?)', iso());
    sql.exec('INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES (\'hetzner\', ?, 1)', iso());
    expect(active()).toBe(0); // Fresh census confirms this exact native is no longer observed.
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');

    sql.exec('UPDATE fleet_catalog_seen SET observed_at = ? WHERE machine_id = \'hetzner\'', iso(-180001));
    sql.exec('UPDATE fleet_catalog_census SET observed_at = ? WHERE machine_id = \'hetzner\'', iso(-180001));
    expect(active()).toBe(1); // Stale census cannot release the slot.

    sql.exec('UPDATE fleet_catalog_seen SET observed_at = ? WHERE machine_id = \'hetzner\'', iso());
    sql.exec('UPDATE fleet_catalog_census SET observed_at = ? WHERE machine_id = \'hetzner\'', iso());
    sql.exec("UPDATE fleet_sessions SET phase = 'unknown' WHERE machine_id = 'hetzner' AND native_id = 'gone'");
    expect(active()).toBe(1); // Fresh but unknown native state remains fail-closed.
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
  }));

  it('projects an uncertain launch as physically absent only through its exact binding and fresh complete census', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain', native_id=NULL WHERE id=?", launch.id);
    const control = new SessionControlService(sql, cb => cb(), () => now); control.init();
    const nativeId = 'bound-native-gone';
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      'control-' + launch.id, 'test-admin', 'control-' + launch.id, '{}', launch.id, 'hetzner', 'codex', 'headless', 'uncertain',
      JSON.stringify({ native_id: nativeId, native_created_at_ms: now }), iso(), iso());
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner',?,'task-hub-' || ?, 'codex','unobserved',?,'demo','shared-codex',?)",
      nativeId, launch.id, iso(), now);
    const active = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active;

    expect(active()).toBe(1); // Missing census is fail-closed.
    sql.exec("INSERT INTO fleet_catalog_census (machine_id,observed_at,complete) VALUES ('hetzner',?,1)", iso(-180001));
    expect(active()).toBe(1); // Stale complete census is insufficient.
    sql.exec("UPDATE fleet_catalog_census SET observed_at=?,complete=0 WHERE machine_id='hetzner'", iso());
    expect(active()).toBe(1); // Fresh incomplete census is insufficient.
    sql.exec("UPDATE fleet_catalog_census SET observed_at=?,complete=1 WHERE machine_id='hetzner'", iso());
    expect(active()).toBe(0); // Exact bound native plus fresh complete absence releases only physical capacity.
    expect(sql.exec('SELECT state,native_id FROM fleet_launches WHERE id=?', launch.id).one()).toMatchObject({ state: 'uncertain', native_id: null });
    expect(sql.exec('SELECT state,binding_json FROM session_control_sessions WHERE id=?', 'control-' + launch.id).one()).toMatchObject({
      state: 'uncertain', binding_json: JSON.stringify({ native_id: nativeId, native_created_at_ms: now }),
    });

    sql.exec("UPDATE fleet_sessions SET phase='unknown' WHERE machine_id='hetzner' AND native_id=?", nativeId);
    expect(active()).toBe(1); // Unknown phase remains reserved despite the complete census.
    sql.exec("UPDATE fleet_sessions SET phase='unobserved' WHERE machine_id='hetzner' AND native_id=?", nativeId);
    sql.exec("UPDATE fleet_sessions SET created_at_ms=? WHERE machine_id='hetzner' AND native_id=?", now + 1, nativeId);
    expect(active()).toBe(1); // A mismatched native incarnation cannot release this launch.
    sql.exec("UPDATE fleet_sessions SET created_at_ms=? WHERE machine_id='hetzner' AND native_id=?", now, nativeId);
    sql.exec("UPDATE fleet_sessions SET engine='claude' WHERE machine_id='hetzner' AND native_id=?", nativeId);
    expect(active()).toBe(1); // A mismatched engine cannot release this launch.
    sql.exec("UPDATE fleet_sessions SET engine='codex',account='desktop-codex' WHERE machine_id='hetzner' AND native_id=?", nativeId);
    expect(active()).toBe(1); // A mismatched account cannot release this launch.
    sql.exec("UPDATE fleet_sessions SET account='shared-codex' WHERE machine_id='hetzner' AND native_id=?", nativeId);
    sql.exec("UPDATE session_control_sessions SET machine_id='win35' WHERE id=?", 'control-' + launch.id);
    expect(active()).toBe(1); // A binding on another machine cannot release this launch.
    sql.exec("UPDATE session_control_sessions SET machine_id='hetzner',engine='claude' WHERE id=?", 'control-' + launch.id);
    expect(active()).toBe(1); // A mismatched control binding engine cannot release this launch.
    sql.exec("UPDATE session_control_sessions SET engine='codex',binding_json=? WHERE id=?", JSON.stringify({ native_id: nativeId, native_created_at_ms: now + 1 }), 'control-' + launch.id);
    expect(active()).toBe(1); // A mismatched bound creation time cannot release this launch.
    sql.exec("UPDATE session_control_sessions SET binding_json=? WHERE id=?", JSON.stringify({ native_id: 'different-native', native_created_at_ms: now }), 'control-' + launch.id);
    expect(active()).toBe(1); // A different bound native cannot release this launch.
    sql.exec("UPDATE session_control_sessions SET binding_json=NULL WHERE id=?", 'control-' + launch.id);
    expect(active()).toBe(1); // An unbound uncertain launch remains reserved.
    sql.exec("UPDATE session_control_sessions SET binding_json=?,state='uncertain' WHERE id=?", JSON.stringify({ native_id: nativeId, native_created_at_ms: now }), 'control-' + launch.id);
    sql.exec("UPDATE fleet_launches SET state='starting' WHERE id=?", launch.id);
    expect(active()).toBe(1); // An in-flight start remains reserved even with stale absence history.
  }));

  it('keeps uncertain direct-native reservations when the unobserved row has a different engine or account', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain', native_id='direct-native' WHERE id=?", launch.id);
    sql.exec("INSERT INTO fleet_catalog_census (machine_id,observed_at,complete) VALUES ('hetzner',?,1)", iso());
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','direct-native',?,'claude','unobserved',?,'demo','shared-codex',?)",
      'task-hub-' + launch.id, iso(), now);
    const active = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active;

    expect(active()).toBe(1); // Direct uncertain native IDs lack an incarnation-checked binding.
    sql.exec("UPDATE fleet_sessions SET engine='codex', account='desktop-codex' WHERE native_id='direct-native'");
    expect(active()).toBe(1); // Account mismatch cannot release the uncertain reservation either.
  }));

  it('keeps an uncertain reservation when more than one exact control binding matches', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state='uncertain', native_id=NULL WHERE id=?", launch.id);
    const service = new SessionControlService(sql, cb => cb(), () => now); service.init();
    const nativeId = 'ambiguous-bound-native';
    const binding = JSON.stringify({ native_id: nativeId, native_created_at_ms: now });
    for (const id of ['control-first', 'control-second']) {
      sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        id + '-' + launch.id, 'test-admin', id + '-' + launch.id, '{}', launch.id, 'hetzner', 'codex', 'headless', 'uncertain', binding, iso(), iso());
    }
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner',?,'task-hub-' || ?, 'codex','unobserved',?,'demo','shared-codex',?)",
      nativeId, launch.id, iso(), now);
    sql.exec("INSERT INTO fleet_catalog_census (machine_id,observed_at,complete) VALUES ('hetzner',?,1)", iso());

    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active).toBe(1);
  }));

  it.each([
    ['legacy census at the row cap', undefined, 500],
    ['explicitly incomplete census below the row cap', false, 0],
  ])('does not free an unobserved reservation after an %s', (label, censusComplete, rowCount) => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'gone' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','gone','task','codex','unobserved',?,'/repo','shared-codex',?)", iso(), now);
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?)", iso());
    sql.exec("INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1)", iso());
    const active = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one()).active;
    expect(active()).toBe(0);

    const sessions = Array.from({ length: rowCount }, (_, i) => ({
      native_id: `history-${i}`, tag: `history-${i}`, engine: 'codex', phase: 'failed',
      agent_state: null, workspace: '/repo', profile: null, account: null,
    }));
    const body: any = { sessions };
    if (censusComplete !== undefined) body.census_complete = censusComplete;
    expect((await f.handle(request('/sessions', body, 'PUT'), host('hetzner'))).status).toBe(200);

    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE machine_id='hetzner' AND native_id='gone'").one().phase).toBe('unobserved');
    expect(sql.exec("SELECT complete FROM fleet_catalog_census WHERE machine_id='hetzner'").one().complete).toBe(0);
    expect(active()).toBe(1); // Incomplete freshness invalidates the older complete marker.
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
  }));

  it('does not mark an absent native unobserved when the host labels its census incomplete', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'still-live' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','still-live','task','codex','running',?,'/repo','shared-codex',?)", iso(), now);
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?)", iso());
    sql.exec("INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1)", iso());

    const body = { sessions: [], census_complete: false };
    expect((await f.handle(request('/sessions', body, 'PUT'), host('hetzner'))).status).toBe(200);

    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE machine_id='hetzner' AND native_id='still-live'").one().phase).toBe('running');
    expect(sql.exec("SELECT complete FROM fleet_catalog_census WHERE machine_id='hetzner'").one().complete).toBe(0);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active).toBe(1);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
  }));

  it('records an incomplete census before draining capacity waiters', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'still-live' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','still-live','task','codex','unobserved',?,'/repo','shared-codex',?)", iso(), now);

    const waiting: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(waiting.state).toBe('queued');
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?)", iso());
    sql.exec("INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1)", iso());

    const report = await f.handle(request('/sessions', { sessions: [], census_complete: false }, 'PUT'), host('hetzner'));
    expect(report.status).toBe(200);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE machine_id='hetzner' AND native_id='still-live'").one().phase).toBe('unobserved');
    expect(sql.exec("SELECT complete FROM fleet_catalog_census WHERE machine_id='hetzner'").one().complete).toBe(0);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active).toBe(1);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', waiting.id).one().state).toBe('queued');
  }));

  it('keeps legacy sub-cap reports complete and accepts an explicit complete terminal-history truncation', () => fixture(async (f, sql) => {
    await configure(f);
    const launch: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    sql.exec("UPDATE fleet_launches SET state = 'running', native_id = 'gone' WHERE id = ?", launch.id);
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,workspace,account,created_at_ms) VALUES ('hetzner','gone','task','codex','running',?,'/repo','shared-codex',?)", iso(), now);

    expect((await f.handle(request('/sessions', { sessions: [] }, 'PUT'), host('hetzner'))).status).toBe(200);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE native_id='gone'").one().phase).toBe('unobserved');
    expect(sql.exec("SELECT complete FROM fleet_catalog_census WHERE machine_id='hetzner'").one().complete).toBe(1);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active).toBe(0);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');

    sql.exec("UPDATE fleet_sessions SET phase='running' WHERE native_id='gone'");
    // Simulate the production phase-update path, which patches the catalog and
    // invalidates this machine's in-memory census snapshot with notePhase.
    f.resources.notePhase('hetzner', 'gone', 'running', iso());
    const terminalHistory = Array.from({ length: 500 }, (_, i) => ({
      native_id: `history-${i}`, tag: `history-${i}`, engine: 'codex', phase: 'failed',
      agent_state: null, workspace: '/repo', profile: null, account: null,
    }));
    expect((await f.handle(request('/sessions', { sessions: terminalHistory, census_complete: true }, 'PUT'), host('hetzner'))).status).toBe(200);
    expect(sql.exec("SELECT phase FROM fleet_sessions WHERE native_id='gone'").one().phase).toBe('unobserved');
    expect(sql.exec("SELECT complete FROM fleet_catalog_census WHERE machine_id='hetzner'").one().complete).toBe(1);
    expect(f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).active).toBe(0);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('running');
  }));

  it('ignores stale progress on orphan workloads while preserving unknown activity holds', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?)", iso());
    sql.exec("INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1)", iso());
    sql.exec("INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,observed_at,last_activity_at,workspace,account) VALUES ('hetzner','orphan','orphan','codex','running',?,?,'/repo','shared-codex')", iso(), iso(-7 * 3600000));
    const account = sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one();
    expect(f.accountStatus(account).active).toBe(0);
    sql.exec("UPDATE fleet_sessions SET observed_at = ? WHERE native_id = 'orphan'", iso(1000));
    expect(f.accountStatus(account).active).toBe(0);
    sql.exec("UPDATE fleet_sessions SET last_activity_at = NULL WHERE native_id = 'orphan'");
    expect(f.accountStatus(account).active).toBe(1);
  }));

  it('derives active concurrency from observed live sessions, expires unmatched reservations after TTL, and refuses genuinely full capacity', () => fixture(async (f, sql) => {
    await configure(f);
    sql.exec("UPDATE fleet_accounts SET max_concurrency = 2 WHERE label = 'shared-codex'");
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?), ('win35', ?)", iso(), iso());
    sql.exec("INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1), ('win35', ?, 1)", iso(), iso());
    const status = () => f.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label = 'shared-codex'").one());

    // 1. Reservations without a matching observed native session hold a slot within TTL (15m) and expire past TTL.
    sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES ('unmatched-offered','admin','human','offered','k-off','{}','hetzner','codex','shared-codex','headless','demo','goal','offered',NULL,?,?)`, iso(-20 * 60000), iso(-5 * 60000));
    sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES ('unmatched-running','admin','human','running-no-native','k-run','{}','hetzner','codex','shared-codex','headless','demo','goal','running',NULL,?,?)`, iso(-20 * 60000), iso(-5 * 60000));
    expect(status()).toMatchObject({ active: 2, reserved: 2, observed_sessions: 0, available_slots: 0 });

    // Once updated_at ages past the 15-minute reservation TTL without a live native session, both expire.
    sql.exec("UPDATE fleet_launches SET updated_at = ? WHERE id IN ('unmatched-offered', 'unmatched-running')", iso(-16 * 60000));
    expect(status()).toMatchObject({ active: 0, reserved: 0, observed_sessions: 0, available_slots: 2 });

    // 2. Bound running launch with a live observed native session counts in observed_sessions (not reserved), even when launch.updated_at is old.
    sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES ('bound-live','admin','human','bound','k-bound','{}','hetzner','codex','shared-codex','headless','demo','goal','running','native-1',?,?)`, iso(-2 * 3600000), iso(-2 * 3600000));
    sql.exec(`INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,last_activity_at,workspace,account)
      VALUES ('hetzner','native-1','task-hub-bound-live','codex','running','working',?,?,'demo','shared-codex')`, iso(), iso());
    expect(status()).toMatchObject({ active: 1, reserved: 0, observed_sessions: 1, native_live: 1, available_slots: 1 });

    // 3. Adding a second observed live session fills max_concurrency = 2 -> genuinely full capacity refuses placement with account_concurrency.
    sql.exec(`INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,last_activity_at,workspace,account)
      VALUES ('hetzner','native-2','manual-worker','codex','running','working',?,?,'demo','shared-codex')`, iso(), iso());
    expect(status()).toMatchObject({ active: 2, reserved: 0, observed_sessions: 2, native_live: 2, available_slots: 0 });
    expect(status().placement_reasons).toContain('account_concurrency');

    const planned: any = f.plan(intent({ machine: 'hetzner', account: 'shared-codex' }));
    expect(planned.selected).toBeNull();
    expect(planned.rejected).toEqual(expect.arrayContaining([
      expect.objectContaining({ machine: 'hetzner', reason: 'account_concurrency', account: 'shared-codex', concurrency: { active: 2, limit: 2, available: 0 } }),
    ]));
    const held: any = await (await f.handle(request('/launches', intent({ machine: 'hetzner', account: 'shared-codex' })), admin)).json();
    expect(held.state).toBe('queued');
    expect(held.rejected).toEqual(expect.arrayContaining([expect.objectContaining({ machine: 'hetzner', reason: 'account_concurrency' })]));
  }));
});


describe('quota probe staleness alert', () => {
  it('raises only when every server observation is older than 30 minutes', () => fixture(async (_f, sql) => {
    sql.exec('CREATE TABLE IF NOT EXISTS server_quota_snapshots (account TEXT PRIMARY KEY, provider TEXT NOT NULL, captured_at TEXT NOT NULL, quota_json TEXT NOT NULL)');
    expect(quotaProbeStaleAlerts(sql, now)).toEqual([]);
    sql.exec("INSERT INTO server_quota_snapshots VALUES ('a','codex',?, '{}')", iso(-31 * 60000));
    sql.exec("INSERT INTO server_quota_snapshots VALUES ('b','go',?, '{}')", iso(-90 * 60000));
    const alerts = quotaProbeStaleAlerts(sql, now);
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({ kind: 'agent', id: 'quota-server', state: 'stale' });
    expect(alerts[0].detail).toContain('older than 30m');
    sql.exec("UPDATE server_quota_snapshots SET captured_at = ? WHERE account = 'b'", iso(-60000));
    expect(quotaProbeStaleAlerts(sql, now)).toEqual([]);
  }));
});
