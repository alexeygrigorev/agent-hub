import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { HostFailoverService } from '../src/host-failover';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const sid = '11111111-1111-4111-8111-111111111111';
const taskId = '22222222-2222-4222-8222-222222222222';
const at = Date.parse('2026-10-30T07:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();

async function fixture(fn: (f: any) => Promise<void>) {
  const b = env as unknown as Env;
  await runInDurableObject(b.HUB.get(b.HUB.idFromName('failover-' + crypto.randomUUID())), async (hub: Hub, state: DurableObjectState) => {
    const sql = hub.sql, clock = { now: at }, tx = <T>(f: () => T) => state.storage.transactionSync(f);
    const sc = new SessionControlService(sql, tx, () => clock.now); sc.init();
    const svc = new HostFailoverService(sql, tx, sc, () => clock.now); svc.init();
    const task = { id: taskId, project: 'agent-hub', title: 'work', status: 'open', version: 1,
      acceptance_criteria: 'AC1 do it', history: [], created_at: at, updated_at: at };
    sql.exec('INSERT INTO intake_tasks VALUES(?,?)', taskId, JSON.stringify(task));
    sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('tabo','tabo','tabo',?,?)", iso(at), iso(at));
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES(?,'operator','k',?,'tabo','codex','interactive','running',?,?,?)`, sid,
      JSON.stringify({ intake_task_id: taskId, workspace: 'agent-hub' }),
      JSON.stringify({ native_id: sid, tag: 't', workspace: '/w', native_created_at_ms: at }), iso(at), iso(at));
    await fn({ sql, clock, svc, sc });
  });
}
const dead = (sql: any) => sql.exec('SELECT * FROM host_failover').toArray();

describe('host failover', () => {
  it('does not mark a host dead before 60 minutes', async () => fixture(async ({ sql, clock, svc }: any) => {
    clock.now = at + 59 * 60000;
    await svc.tick();
    expect(dead(sql)).toEqual([]);
    expect(svc.summary()).toEqual([]);
  }));
  it('marks a host dead after 60 minutes and reassigns its open-intake session exactly once', async () => fixture(async ({ sql, clock, svc }: any) => {
    clock.now = at + 61 * 60000;
    await svc.tick();
    await svc.tick();
    expect(dead(sql)).toMatchObject([{ machine_id: 'tabo', state: 'temporarily_dead' }]);
    expect(svc.summary()).toMatchObject([{ machine_id: 'tabo', state: 'temporarily_dead', reassigned_sessions: 1 }]);
    const rows = sql.exec('SELECT * FROM host_failover_sessions').toArray();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ session_id: sid, state: 'reassigned' });
    const replacements = sql.exec("SELECT id FROM session_control_sessions WHERE id<>?", sid).toArray();
    expect(replacements.map((r: any) => r.id)).toEqual([rows[0].replacement_id]);
  }));
  it('ignores sessions without open intake work', async () => fixture(async ({ sql, clock, svc }: any) => {
    sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.status','done')");
    clock.now = at + 61 * 60000;
    await svc.tick();
    expect(dead(sql)).toHaveLength(1);
    expect(sql.exec('SELECT * FROM host_failover_sessions').toArray()).toEqual([]);
  }));
  it('fences the old session with a stop and recovers the host on return', async () => fixture(async ({ sql, clock, svc }: any) => {
    clock.now = at + 61 * 60000;
    await svc.tick();
    sql.exec("UPDATE machines SET last_checkin_at=? WHERE id='tabo'", iso(clock.now + 30000));
    clock.now += 60000;
    await svc.tick();
    const fence = sql.exec("SELECT * FROM session_control_commands WHERE session_id=? AND kind='stop'", sid).toArray();
    expect(fence).toHaveLength(1);
    expect(svc.summary()).toEqual([]);
    expect(sql.exec('SELECT state FROM host_failover_sessions').toArray()[0].state).toBe('fencing');
    await svc.tick();
    expect(sql.exec("SELECT COUNT(*) n FROM session_control_commands WHERE session_id=? AND kind='stop'", sid).toArray()[0].n).toBe(1);
    sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sid);
    await svc.tick();
    expect(sql.exec('SELECT state FROM host_failover_sessions').toArray()[0].state).toBe('fenced');
    expect(dead(sql)[0].state).toBe('recovered');
  }));
});
