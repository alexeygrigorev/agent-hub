import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const revoke = (fleet: FleetService, id: string, body: unknown, auth: any = admin) =>
  fleet.handle(new Request(`https://hub.test/api/fleet/machines/${id}/revoke`, { method: 'POST', body: JSON.stringify(body) }), auth);

async function fixture(check: (fleet: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('machine-revoke-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    fleet.init();
    const sql = hub.sql;
    const heartbeat = { mem_avail_mb: 37000, disk_free_gb: 80, load: [1, 1, 1], cpu_usage: freshCpu(iso(), 8, 8) };
    for (const id of ['good', 'leaked']) {
      sql.exec('INSERT INTO credentials (id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        'cred-' + id, 'host', 'th-' + id, 'rh-' + id, 'fam-' + id, id, id, 'machine:' + id, '["host"]', 'admin', iso(), iso(86400000));
      sql.exec('INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,credential_id,capabilities) VALUES (?,?,?,?,?,?,?,?,?)',
        id, id, id.toUpperCase(), iso(), iso(), JSON.stringify(heartbeat), 0, 'cred-' + id,
        JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO routing_machines (machine_id,needs_json,max_agents,runners_json,observed_at) VALUES (?,?,?,?,?)', id, '[]', 1, '[]', iso());
    }
    await check(fleet, sql);
  });
}

describe('hub machines revoke (intake 298e50f9)', () => {
  it('requires admin, confirm and a reason, and changes nothing otherwise', async () => fixture(async (fleet, sql) => {
    expect((await revoke(fleet, 'leaked', { confirm: true, reason: 'x' }, { caller: { kind: 'host', machineId: 'leaked' }, scopes: ['host'] })).status).toBe(403);
    expect((await revoke(fleet, 'leaked', { reason: 'x' })).status).toBe(400);
    expect((await revoke(fleet, 'leaked', { confirm: true })).status).toBe(400);
    expect((await revoke(fleet, 'leaked', { confirm: true, reason: '  ' })).status).toBe(400);
    expect((await revoke(fleet, 'nope', { confirm: true, reason: 'x' })).status).toBe(404);
    expect(sql.exec('SELECT revoked_at FROM machines WHERE id=?', 'leaked').one().revoked_at).toBeNull();
    expect(sql.exec('SELECT revoked_at FROM credentials WHERE id=?', 'cred-leaked').one().revoked_at).toBeNull();
  }));

  it('kills credentials, hides the machine, audits, and keeps history rows', async () => fixture(async (fleet, sql) => {
    sql.exec('INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,account,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
      'leaked', 'n1', 't1', 'codex', 'running', 'idle', iso(), 'demo', null, iso());
    const res = await revoke(fleet, 'leaked', { confirm: true, reason: 'unknown/test host' });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ status: 'revoked', machine_id: 'leaked', credentials_revoked: 1 });
    expect(sql.exec('SELECT revoked_at FROM credentials WHERE id=?', 'cred-leaked').one().revoked_at).not.toBeNull();
    expect(sql.exec('SELECT revoked_at FROM credentials WHERE id=?', 'cred-good').one().revoked_at).toBeNull();
    const audit = sql.exec("SELECT actor,target,detail FROM audit WHERE action='machine_revoke'").toArray();
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ actor: 'test-admin', target: 'leaked' });
    expect(audit[0].detail).toContain('unknown/test host');
    // history retained
    expect(sql.exec('SELECT COUNT(*) AS n FROM machines WHERE id=?', 'leaked').one().n).toBe(1);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_sessions WHERE machine_id=?', 'leaked').one().n).toBe(1);
    // idempotent
    expect(await (await revoke(fleet, 'leaked', { confirm: true, reason: 'again' })).json()).toMatchObject({ status: 'already_revoked' });
    expect(sql.exec("SELECT COUNT(*) AS n FROM audit WHERE action='machine_revoke'").one().n).toBe(1);
  }));

  it('is excluded from fleet views and never placeable', async () => fixture(async (fleet, sql) => {
    await revoke(fleet, 'leaked', { confirm: true, reason: 'r' });
    const view: any = await (await fleet.handle(new Request('https://hub.test/api/fleet'), admin)).json();
    expect(view.machines.map((m: any) => m.id)).toEqual(['good']);
    expect(sql.exec('SELECT id FROM machines WHERE revoked_at IS NULL').toArray().map((r: any) => r.id)).toEqual(['good']);
    const plan = fleet.plan({ machine: 'leaked', workspace: 'demo', prompt: 'x', idempotency_key: crypto.randomUUID() });
    expect(plan.selected).toBeFalsy();
  }));
});
