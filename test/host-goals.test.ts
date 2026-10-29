import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { HostGoals } from '../src/host-goals';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

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
    sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account) VALUES('tabo','native','proof','claude','running',?,'tasks','claude-test')", at);
    expect(goals.view('tabo').state).toBe('done');
    goals.reconcile();
    sql.exec("UPDATE fleet_sessions SET phase='exited'");
    sql.exec("UPDATE fleet_launches SET state='exited'");
    expect(goals.view('tabo').engines[0].verified_at).toBe(at);
    expect(goals.view('tabo').state).toBe('done');
    sql.exec("UPDATE engine_bundles SET revision='rev2'");
    expect(goals.view('tabo').state).toBe('bringing_up');
  });
});
