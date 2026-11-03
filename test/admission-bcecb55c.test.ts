import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, UNCLAIMED_OFFER_TTL_MS } from '../src/fleet';
import { freshCpu } from './cpu-fixture';

const now = Date.parse('2026-11-01T10:00:00Z');
const iso = (delta = 0) => new Date(now + delta).toISOString();
const hostAuth = (machine = 'online') => ({ caller: { kind: 'host', actor: machine, machineId: machine }, scopes: ['host'] });
const adminAuth = () => ({ caller: { kind: 'human', isAdmin: true, actor: 'admin' }, scopes: ['admin'] });

async function setup(fn: (s: FleetService, sql: any) => Promise<void> | void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('admission-bcecb55c-' + crypto.randomUUID()));
  await (runInDurableObject as any)(stub, async (h: any, state: DurableObjectState) => {
    const s = new FleetService(h.sql, (cb: any) => state.storage.transactionSync(cb), () => now);
    s.init();
    const sql = h.sql;
    const heartbeat = JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });
    sql.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities)
      VALUES('online','online','online',?,?,?,0,?)`, iso(), iso(), heartbeat,
      JSON.stringify({ engines: ['zcodex'], fleet_modes: ['headless'], workspaces: ['real-project'], cpu_count: 8 }));
    sql.exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('zai-main','zai',10,15,1)");
    sql.exec("INSERT INTO fleet_routes(machine_id,engine,account,profile) VALUES('online','zcodex','zai-main',NULL)");
    sql.exec("INSERT INTO fleet_catalog_census(machine_id,observed_at,complete) VALUES('online',?,1)", iso());
    await fn(s, sql);
  });
}

function offerLaunch(sql: any, id: string, state = 'offered', updatedAt = iso()) {
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,model_selection_json)
    VALUES(?,'test','human','bcecb55c',?,'{}','online','zcodex','zai-main','headless','real-project','',?,?,?,?)`,
    id, id, state, updatedAt, updatedAt, JSON.stringify({ model: 'test-model' }));
}

function linkSession(sql: any, sessionId: string, launchId: string, sessionState: string, updatedAt = iso()) {
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at)
    VALUES(?,'test',?,'{}',?,'online','zcodex','headless',?,?,?)`, sessionId, sessionId, launchId, sessionState, updatedAt, updatedAt);
}

const claimBody = (observedAt: string) => JSON.stringify({ gate_allowed: true, observed_at: observedAt, account: 'zai-main', profile_supported: true, model: 'test-model', claim_key: 'claim-key-1' });

describe('hub admission bugs bcecb55c', () => {
  it('AC2: a cancelled session fences the queued->placing race (drain never offers it)', async () => {
    await setup(async (s, sql) => {
      const launch = crypto.randomUUID(), session = crypto.randomUUID();
      offerLaunch(sql, launch, 'queued');
      linkSession(sql, session, launch, 'cancelled');
      const before = (sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch).one() as any).state;
      expect(before).toBe('queued');
      s.drainCapacityQueue();
      const after: any = sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch).one();
      expect(after.state).not.toBe('offered');
      expect(after.state).toBe('cancelled');
      // A late host claim on the fenced launch cannot start it.
      const res = await s.handle(new Request('https://hub.test/api/fleet/launches/' + launch + '/claim', { method: 'POST', body: claimBody(iso()) }), hostAuth() as any);
      expect(res.status).toBe(409);
      expect(((await res.json()) as any).error).toBe('already_claimed');
      expect((sql.exec('SELECT state FROM session_control_sessions WHERE id=?', session).one() as any).state).toBe('cancelled');
    });
  });

  it('AC2: cancel wins after drain promotes queued->offered; a won claim refuses cancel atomically', async () => {
    await setup(async (s, sql) => {
      void s;
      // Drain promotes queued -> offered; the operator cancel below must still match.
      const promoted = crypto.randomUUID();
      offerLaunch(sql, promoted, 'queued');
      const cancelled: any = sql.exec(`UPDATE fleet_launches SET state='cancelled', verification_status='not_started', exit_reason='session_cancelled', finished_at=?, updated_at=? WHERE id=? AND state IN ('queued','offered') AND native_id IS NULL AND launcher_task_id IS NULL RETURNING id`, iso(), iso(), promoted).toArray();
      expect(cancelled.length).toBe(1);
      // A claim that won first (starting with a claim key) is no longer cancellable: no-start is unproven.
      const claimed = crypto.randomUUID();
      offerLaunch(sql, claimed, 'starting');
      sql.exec('UPDATE fleet_launches SET claim_key=? WHERE id=?', 'claim-key-1', claimed);
      const loser: any = sql.exec(`UPDATE fleet_launches SET state='cancelled', verification_status='not_started', exit_reason='session_cancelled', finished_at=?, updated_at=? WHERE id=? AND state IN ('queued','offered') AND native_id IS NULL AND launcher_task_id IS NULL RETURNING id`, iso(), iso(), claimed).toArray();
      expect(loser.length).toBe(0);
    });
  });

  it('AC3: a fresh-admission refusal is recorded in rejected[] and the offer expires within the bound', async () => {
    await setup(async (s, sql) => {
      const launch = crypto.randomUUID(), session = crypto.randomUUID();
      offerLaunch(sql, launch, 'offered');
      linkSession(sql, session, launch, 'offered');
      const res = await s.handle(new Request('https://hub.test/api/fleet/launches/' + launch + '/claim', { method: 'POST', body: claimBody(iso(-60000)) }), hostAuth() as any);
      expect(res.status).toBe(409);
      expect(((await res.json()) as any).error).toBe('fresh_admission_required');
      const held: any = sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?', launch).one();
      expect(JSON.parse(held.rejected_json)).toEqual([{ machine: 'online', engine: 'zcodex', reason: 'fresh_admission_required' }]);
      const status: any = await (await s.handle(new Request('https://hub.test/api/fleet', { method: 'GET' }), adminAuth() as any)).json();
      const row = (status.launches as any[]).find(r => r.id === launch);
      expect(row.state).toBe('offered');
      expect(row.rejected).toEqual([{ machine: 'online', engine: 'zcodex', reason: 'fresh_admission_required' }]);
      // Age the offer past the unclaimed-offer bound: it must fail and requeue, carrying the refusal evidence.
      const old = new Date(now - UNCLAIMED_OFFER_TTL_MS - 1000).toISOString();
      sql.exec('UPDATE fleet_launches SET updated_at=?, created_at=? WHERE id=?', old, old, launch);
      sql.exec('UPDATE session_control_sessions SET updated_at=? WHERE id=?', old, session);
      expect(s.expireUnclaimedOffers()).toBe(1);
      expect((sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch).one() as any).state).toBe('failed');
      const retry: any = sql.exec("SELECT * FROM fleet_launches WHERE state='queued'").one();
      expect(retry).toBeTruthy();
      const retryHold: any = sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?', retry.id).one();
      const reasons = JSON.parse(retryHold.rejected_json).map((r: any) => r.reason);
      expect(reasons).toContain('fresh_admission_required');
      expect((sql.exec('SELECT state, fleet_launch_id FROM session_control_sessions WHERE id=?', session).one() as any)).toMatchObject({ state: 'queued', fleet_launch_id: retry.id });
    });
  });
});
