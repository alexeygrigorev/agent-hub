import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { HostGoals } from '../src/host-goals';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

it('requires release, installation and observed native running proof; does not trust launch state', async () => {
  const bindings = env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName(crypto.randomUUID())), (hub: Hub) => {
    const now = Date.now(), at = new Date(now).toISOString(), sha = 'a'.repeat(40);
    const sql = hub.sql;
    const goals = new HostGoals(sql, hub.fleetService, () => now);
    goals.init();
    sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES(?,?,?,?,?,?)', 'tabo', 'Tabo', 'tabo', at, at, JSON.stringify({ workspaces: ['tasks'] }));
    sql.exec('INSERT INTO engine_bundles VALUES(?,?,?,?,?)', 'claude-test', 'claude', '1.0.0', 'rev1', '{}');
    expect(goals.set('tabo', { engines: ['absent'], workspace: 'tasks', sha })).toEqual({ error: 'engine_bundle_missing' });
    const goal = goals.set('tabo', { engines: ['claude-test'], workspace: 'tasks', sha });
    expect(goal.state).toBe('updating_release');
    expect(goals.set('tabo', { engines: ['claude-test'], workspace: 'tasks', sha }).generation).toBe(goal.generation);
    sql.exec("UPDATE host_release_targets SET current_sha=?,state='current',reported_at=? WHERE machine_id='tabo'", sha, at);
    expect(goals.view('tabo').engines[0].state).toBe('installing');
    sql.exec('INSERT INTO engine_installations VALUES(?,?,?)', 'tabo', 'claude-test', 'rev1');
    expect(goals.view('tabo').state).toBe('bringing_up');
    sql.exec("INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES('proof','hub-host-goal','human','verification','key','{}','tabo','claude','claude-test','headless','tasks','verify','running','native',?,?)", at, at);
    sql.exec('INSERT INTO host_goal_proofs(generation,label,revision,launch_id) VALUES(?,?,?,?)', goal.generation, 'claude-test', 'rev1', 'proof');
    expect(goals.view('tabo').engines[0].state).toBe('verifying_native');
    sql.exec("UPDATE fleet_launches SET state='starting' WHERE id='proof'");
    sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account) VALUES('tabo','native','proof','claude','running',?,'tasks','claude-test')", at);
    expect(goals.view('tabo').engines[0].verified_at).toBeNull();
    expect(goals.view('tabo').state).toBe('bringing_up');
    sql.exec("UPDATE fleet_launches SET state='running' WHERE id='proof'");
    expect(goals.view('tabo').state).toBe('done');
    // No maintenance tick is needed to retain a short session's running proof.
    sql.exec("UPDATE fleet_sessions SET phase='exited'");
    sql.exec("UPDATE fleet_launches SET state='exited'");
    expect(goals.view('tabo').engines[0].verified_at).toBe(at);
    expect(goals.view('tabo').state).toBe('done');
    sql.exec("UPDATE engine_bundles SET revision='rev2'");
    expect(goals.view('tabo').state).toBe('bringing_up');
    const retry = goals.set('tabo', { engines: ['claude-test'], workspace: 'tasks', sha, retry: true });
    expect(retry.generation).not.toBe(goal.generation);
    expect(retry.engines[0].verified_at).toBeNull();
  });
});

it('places verification through normal admission on an interactive-only host', async () => {
  const bindings = env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
    const now = Date.now(), at = new Date(now).toISOString(), sha = 'b'.repeat(40);
    const sql = hub.sql, fleet = hub.fleetService;
    const request = (path: string, body: any) => new Request('https://hub.test' + path, { method: 'PUT', body: JSON.stringify(body) });
    const admin = { caller: { isAdmin: true, actor: 'admin', kind: 'human' as const }, scopes: ['admin'] };
    const host = { caller: { kind: 'host' as const, machineId: 'interactive' }, scopes: ['host'] };
    sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities,last_heartbeat) VALUES(?,?,?,?,?,?,?)',
      'interactive', 'interactive', 'interactive', at, at,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['interactive'], workspaces: ['tasks'], cpu_count: 8 }),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at), gate: { launch_allowed: true, at } }));
    expect((await fleet.handle(request('/api/fleet/accounts', {
      accounts: [{ label: 'codex-test', provider: 'codex', max_concurrency: 3, reserve_percent: 15, enabled: true }],
      routes: [{ machine_id: 'interactive', engine: 'codex', account: 'codex-test', profile: 'hub-codex-test' }],
    }), admin)).status).toBe(200);
    const reset = new Date(now + 86400000).toISOString();
    expect((await fleet.handle(request('/api/fleet/subscriptions', { subscriptions: [{ service: 'codex', account: 'codex-test', plan: null,
      quota: { status: 'ok', observed_at: at, details: { limit_reached: false }, windows: {
        '5h': { present: true, percent_remaining: 80, reset_at: reset },
        '7d': { present: true, percent_remaining: 80, reset_at: reset },
      } } }] }), host)).status).toBe(200);
    sql.exec('INSERT INTO engine_bundles VALUES(?,?,?,?,?)', 'codex-test', 'codex', '1.0.0', 'revision', '{}');
    sql.exec('INSERT INTO engine_installations VALUES(?,?,?)', 'interactive', 'codex-test', 'revision');
    const goals = new HostGoals(sql, fleet, () => now);
    goals.set('interactive', { engines: ['codex-test'], workspace: 'tasks', sha });
    sql.exec("UPDATE host_release_targets SET current_sha=?,state='current',reported_at=? WHERE machine_id='interactive'", sha, at);
    goals.reconcile();
    const result = goals.view('interactive');
    expect(result.mode).toBe('interactive');
    expect(result.engines[0].launch_id).toBeTruthy();
    expect(sql.exec('SELECT mode FROM fleet_launches WHERE id=?', result.engines[0].launch_id).one().mode).toBe('interactive');
    expect(result.state).toBe('bringing_up');
  });
});
