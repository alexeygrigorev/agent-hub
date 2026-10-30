import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { HELD_RECONCILE_INTERVAL_MS, HELD_UNPLACED_SQL, SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 5551f943: memory_pressure holds were never re-placed after pressure
// cleared because the background reconcile always read the same oldest 20
// held rows, which never-promotable holds filled forever. The pass runs on
// host polls, so it must also stay cheap: indexed, rate-limited, bounded
// reserve attempts, and no writes when nothing changed.
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId: 'host-cred-1' }, scopes: ['host'] });
const PRESSURE = [{ machine: 'hetzner', engine: 'claude', reason: 'memory_pressure' }];
const PERMANENT = [{ machine: 'hetzner', engine: 'grok', reason: 'provider_disabled' }];

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

type Meter = { read: number; written: number };

/** SqlStorage wrapper that sums rows read/written by every statement. */
function metered(sql: any) {
  const cursors: any[] = [];
  const proxy = new Proxy(sql, {
    get(target, prop) {
      if (prop === 'exec') return (q: string, ...args: any[]) => { const c = target.exec(q, ...args); cursors.push(c); return c; };
      const v = target[prop];
      return typeof v === 'function' ? v.bind(target) : v;
    },
  });
  const measure = async (fn: () => any): Promise<Meter> => {
    cursors.length = 0;
    await fn();
    return cursors.reduce((m, c) => ({ read: m.read + c.rowsRead, written: m.written + c.rowsWritten }), { read: 0, written: 0 });
  };
  return { proxy, measure };
}

async function fixture(fn: (s: SessionControlService, sql: any, clock: { t: number }, measure: (fn: () => any) => Promise<Meter>) => Promise<void>,
  reserve: (input: any) => { value: any; status: number }) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('held-reconcile-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { t: Date.UTC(2026, 9, 30, 17, 0, 0) };
    const m = metered(hub.sql);
    const service = new SessionControlService(m.proxy, (cb) => state.storage.transactionSync(cb), () => clock.t, { reserve }, {});
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql, clock, m.measure);
  });
}

/** Creates held interactive sessions; `workspace` tells the fake reserve which hold to return. */
async function createHeld(s: SessionControlService, sql: any, workspace: string, n: number, minute: number): Promise<string[]> {
  const ids: string[] = [];
  for (let i = 0; i < n; i++) {
    const created: any = await (await s.handle(request('/api/sessions', {
      key: `${workspace}-${minute}-${i}`, name: `${workspace}-${minute}-${i}`, workspace, mode: 'interactive',
    }), admin)).json();
    expect(created).toMatchObject({ state: 'held' });
    sql.exec('UPDATE session_control_sessions SET created_at = ? WHERE id = ?', new Date(Date.UTC(2026, 9, 30, 11, minute, i)).toISOString(), created.id);
    ids.push(created.id);
  }
  return ids;
}

const stateOf = (sql: any, id: string) => (sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id).toArray()[0] as any).state;
const nextPass = (clock: { t: number }) => { clock.t += HELD_RECONCILE_INTERVAL_MS; };

function placingReserve(open: () => boolean, onCall?: (input: any) => void) {
  let launches = 0;
  return (input: any) => {
    onCall?.(input);
    if (input.workspace === 'pressured' && open()) {
      launches++;
      return { status: 201, value: { id: 'launch-' + launches, state: 'offered', machine: 'hetzner', engine: 'claude' } };
    }
    return { status: 409, value: { error: 'no_eligible_route', rejected: input.workspace === 'pressured' ? PRESSURE : PERMANENT } };
  };
}

describe('held session reconcile (intake 5551f943)', () => {
  it('re-places a memory_pressure hold queued behind more than a window of never-promotable holds', async () => {
    let open = false;
    await fixture(async (s, sql, clock) => {
      await createHeld(s, sql, 'demo', 25, 0);
      const [target] = await createHeld(s, sql, 'pressured', 1, 1);
      sql.exec('DELETE FROM session_control_held_cursor');
      open = true;
      nextPass(clock);
      expect(s.reconcileHeldSessions().promoted).toBe(0); // 5 permanent holds re-evaluated
      expect(stateOf(sql, target)).toBe('held');
      // Inside the interval the background pass is skipped entirely.
      expect(s.reconcileHeldSessions().promoted).toBe(0);
      // Every hold is re-evaluated (intake e4123b02), so the rotation reaches
      // the pressured row after a bounded number of passes.
      let promoted = 0;
      for (let i = 0; i < 8 && !promoted; i++) { nextPass(clock); promoted = s.reconcileHeldSessions().promoted; }
      expect(promoted).toBe(1);
      expect(stateOf(sql, target)).toBe('offered');
      expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_sessions WHERE state = 'held'").toArray()[0] as any).n).toBe(25);
    }, placingReserve(() => open));
  });

  it('wraps the cursor so every held row is revisited', async () => {
    let open = false;
    await fixture(async (s, sql, clock) => {
      const ids = await createHeld(s, sql, 'pressured', 3, 0);
      sql.exec('DELETE FROM session_control_held_cursor');
      nextPass(clock);
      expect(s.reconcileHeldSessions().promoted).toBe(0);
      open = true;
      nextPass(clock);
      expect(s.reconcileHeldSessions().promoted).toBe(3);
      for (const id of ids) expect(stateOf(sql, id)).toBe('offered');
    }, placingReserve(() => open));
  });

  it('bounds reserve attempts per pass and isolates a throwing row', async () => {
    let open = false;
    const calls: string[] = [];
    let boom: string | null = null;
    await fixture(async (s, sql, clock) => {
      const ids = await createHeld(s, sql, 'pressured', 12, 0);
      sql.exec('DELETE FROM session_control_held_cursor');
      calls.length = 0;
      nextPass(clock);
      s.reconcileHeldSessions();
      expect(calls.length).toBe(5); // persistent pressure: at most `limit` reserve calls per pass
      open = true;
      boom = ids[5];
      calls.length = 0;
      nextPass(clock);
      const pass = s.reconcileHeldSessions();
      expect(stateOf(sql, ids[5])).toBe('held');
      expect(pass.promoted).toBe(4); // the throwing row consumed one attempt; the rest of the pass still ran
      expect(stateOf(sql, ids[6])).toBe('offered');
    }, input => {
      calls.push(input.idempotency_key);
      if (boom && input.idempotency_key === 'session-control-' + boom) throw new Error('reserve exploded');
      return placingReserve(() => open)(input);
    });
  });

  it('a targeted read evaluates only its own held row', async () => {
    const calls: string[] = [];
    await fixture(async (s, sql) => {
      const ids = await createHeld(s, sql, 'pressured', 6, 0);
      calls.length = 0;
      await s.handle(request(`/api/sessions/${ids[3]}`, undefined, 'GET'), admin);
      expect(calls).toEqual(['session-control-' + ids[3]]);
    }, input => { calls.push(input.idempotency_key); return placingReserve(() => false)(input); });
  });

  it('writes nothing when a pass changes nothing', async () => {
    await fixture(async (s, sql, clock, measure) => {
      await createHeld(s, sql, 'pressured', 3, 0);
      await createHeld(s, sql, 'demo', 3, 1);
      for (let i = 0; i < 4; i++) { nextPass(clock); s.reconcileHeldSessions(); } // one full rotation settles hold_json and the cursor
      nextPass(clock);
      const idle = await measure(() => s.reconcileHeldSessions());
      expect(idle.written).toBeLessThanOrEqual(1); // only the rotating cursor
      const skipped = await measure(() => s.reconcileHeldSessions()); // inside the interval
      expect(skipped).toEqual({ read: 0, written: 0 });
    }, placingReserve(() => false));
  });

  it('uses the partial index range scan without a temp B-tree', async () => {
    await fixture(async (_s, sql) => {
      const plan = (q: string, ...a: any[]) => (sql.exec('EXPLAIN QUERY PLAN ' + q, ...a).toArray() as any[]).map(r => r.detail).join('\n');
      const base = HELD_UNPLACED_SQL;
      for (const p of [plan(`${base} ORDER BY created_at, id LIMIT 20`),
        plan(`${base} AND (created_at, id) > (?, ?) ORDER BY created_at, id LIMIT 20`, 'x', 'y'),
        plan(`${base} AND (created_at, id) <= (?, ?) ORDER BY created_at, id LIMIT 20`, 'x', 'y')]) {
        expect(p).toContain('session_control_held_unplaced');
        expect(p).not.toContain('TEMP B-TREE');
      }
    }, placingReserve(() => false));
  });

  it('stays within a rows read/written budget at production scale', async () => {
    await fixture(async (s, sql, clock, measure) => {
      // 5k sessions (mostly terminal), 50k historical launches, 25 permanent + 3 pressure holds.
      sql.exec(`WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c WHERE i < 50000)
        INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at)
        SELECT 'hist-' || i, 'demo:assistant', 'agent', 's', 'k-' || i, '{"workspace":"demo","pad":"' || hex(randomblob(200)) || '"}', 'hetzner', 'claude', 'default', 'headless', 'demo', 'p',
          'exited', '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z' FROM c`);
      sql.exec(`WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c WHERE i < 5000)
        INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at)
        SELECT 'old-' || i, 'demo:assistant', 'rk-' || i, '{"workspace":"demo"}', CASE WHEN i % 3 = 0 THEN NULL ELSE 'hist-' || i END,
          CASE WHEN i % 3 = 0 THEN NULL ELSE 'hetzner' END, 'claude', 'headless', CASE WHEN i % 2 = 0 THEN 'cancelled' ELSE 'failed' END,
          '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z' FROM c`);
      await createHeld(s, sql, 'demo', 25, 0);
      await createHeld(s, sql, 'pressured', 3, 1);
      // Settle: one full rotation normalizes every hold_json once.
      for (let i = 0; i < 8; i++) { nextPass(clock); s.reconcileHeldSessions(); }

      const pass = async () => { nextPass(clock); return measure(() => s.reconcileHeldSessions()); };
      for (let i = 0; i < 3; i++) {
        const m = await pass();
        expect(m.read).toBeLessThanOrEqual(200);
        expect(m.written).toBeLessThanOrEqual(1); // at most the one cursor row
      }
      // Host poll: reconcile at most once per interval; repeat polls inside it add no reconcile work.
      nextPass(clock);
      const first = await measure(() => s.handle(request('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner')));
      const repeat = await measure(() => s.handle(request('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner')));
      expect(first.read - repeat.read).toBeLessThanOrEqual(200);
      expect(repeat.written).toBe(0);
      nextPass(clock);
      const list1 = await measure(() => s.handle(request('/api/sessions?limit=20', undefined, 'GET'), admin));
      const list2 = await measure(() => s.handle(request('/api/sessions?limit=20', undefined, 'GET'), admin));
      expect(list1.read - list2.read).toBeLessThanOrEqual(200);
      expect(list1.written - list2.written).toBeLessThanOrEqual(1);
      console.log('held-reconcile budget', JSON.stringify({ first, repeat, list1, list2 }));
    }, placingReserve(() => false));
  });
});
