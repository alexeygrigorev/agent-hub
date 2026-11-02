import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { HELD_ALERT_MS, HELD_ALERT_INTERVAL_MS, HELD_EXPIRE_MS, HELD_SLOW_INTERVAL_MS, SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake e4123b02: holds for reasons other than memory pressure (engine/probe
// pause, provider disabled, route/account/quota gates) were never re-placed,
// and holds whose owner can never place them were never cancelled or surfaced.
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const REASONS = ['p0-engine-pause-quiet-2', 'provider_disabled', 'route_not_advertised', 'account_blocked', 'quota_window_exhausted', 'workspace_unavailable'];

function request(path: string, data?: any) {
  return new Request('https://hub.test' + path, { method: 'POST', body: JSON.stringify(withFixtureIntake(path, data, 'POST')) });
}

async function fixture(fn: (s: SessionControlService, sql: any, clock: { t: number }, alerts: any[]) => Promise<void>, reserve: (input: any) => { value: any; status: number }) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('held-e4123b02-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { t: Date.UTC(2026, 9, 30, 17, 0, 0) };
    const alerts: any[] = [];
    const setAlert = (kind: string, subject: string, problem: any) => { alerts.push({ kind, subject, problem }); return true; };
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), () => clock.t, { reserve }, { worktreeGuard: { setAlert } });
    service.init();
    seedFixtureIntake(hub.sql);
    await fn(service, hub.sql, clock, alerts);
  });
}

let seq = 0;
async function createHeld(s: SessionControlService, sql: any, workspace: string, ageMs: number, clock: { t: number }): Promise<string> {
  const created: any = await (await s.handle(request('/api/sessions', { key: `${workspace}-${++seq}`, name: `${workspace}-${seq}`, workspace, mode: 'interactive' }), admin)).json();
  expect(created).toMatchObject({ state: 'held' });
  sql.exec('UPDATE session_control_sessions SET created_at = ? WHERE id = ?', new Date(clock.t - ageMs).toISOString(), created.id);
  return created.id;
}
const row = (sql: any, id: string) => sql.exec('SELECT state, hold_json FROM session_control_sessions WHERE id = ?', id).toArray()[0] as any;
const pass = (s: SessionControlService, clock: { t: number }) => { clock.t += HELD_SLOW_INTERVAL_MS; return s.reconcileHeldSessions(20); };
const pausedReserve = (open: () => boolean) => (input: any) => open()
  ? { status: 201, value: { id: 'launch-' + input.idempotency_key, state: 'offered', machine: 'hetzner', engine: 'claude' } }
  : { status: 409, value: { error: 'no_eligible_route', rejected: [{ machine: 'hetzner', engine: 'claude', reason: input.workspace }] } };

describe('stale held session reconcile (intake e4123b02)', () => {
  for (const reason of REASONS) {
    it(`re-places a hold for ${reason} once the cause clears`, async () => {
      let open = false;
      await fixture(async (s, sql, clock) => {
        const id = await createHeld(s, sql, reason, 3600_000, clock);
        sql.exec('DELETE FROM session_control_held_cursor');
        pass(s, clock);
        expect(row(sql, id).state).toBe('held');
        open = true;
        expect(pass(s, clock).promoted).toBe(1);
        expect(row(sql, id).state).toBe('offered');
      }, pausedReserve(() => open));
    });
  }

  it('cancels a hold older than the expiry bound with an explicit reason', async () => {
    await fixture(async (s, sql, clock) => {
      const id = await createHeld(s, sql, 'provider_disabled', HELD_EXPIRE_MS + 1000, clock);
      expect(pass(s, clock).cancelled).toBe(1);
      expect(row(sql, id).state).toBe('cancelled');
      expect(JSON.parse(row(sql, id).hold_json).reason).toBe('held_expired');
    }, pausedReserve(() => false));
  });

  it('cancels a non-transient hold whose owner intake is closed or superseded', async () => {
    await fixture(async (s, sql, clock) => {
      const id = await createHeld(s, sql, 'provider_disabled', 3600_000, clock);
      const taskId = (sql.exec('SELECT id FROM intake_tasks LIMIT 1').toArray()[0] as any).id;
      const task = JSON.parse((sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0] as any).data);
      sql.exec('UPDATE session_control_sessions SET request_json = json_set(request_json, \'$.intake_task_id\', ?) WHERE id = ?', taskId, id);
      sql.exec('UPDATE intake_tasks SET data = ? WHERE id = ?', JSON.stringify({ ...task, status: 'done' }), taskId);
      expect(pass(s, clock).cancelled).toBe(1);
      expect(JSON.parse(row(sql, id).hold_json).reason).toBe('intake_task_closed');
    }, pausedReserve(() => false));
  });

  it('raises ONE deduped alert for stale holds and clears it when none remain', async () => {
    await fixture(async (s, sql, clock, alerts) => {
      const a = await createHeld(s, sql, 'provider_disabled', HELD_ALERT_MS + 3600_000, clock);
      await createHeld(s, sql, 'route_not_advertised', HELD_ALERT_MS + 7200_000, clock);
      await createHeld(s, sql, 'route_not_advertised', 60_000, clock); // fresh: not counted
      pass(s, clock); pass(s, clock); pass(s, clock);
      const raised = alerts.filter(x => x.problem);
      expect(raised.length).toBe(1);
      expect(raised[0]).toMatchObject({ kind: 'held_sessions', subject: 'unplaced' });
      expect(raised[0].problem.detail).toContain('2 unplaced held');
      expect(raised[0].problem.detail).toContain('provider_disabled=1');
      sql.exec("UPDATE session_control_sessions SET state = 'cancelled' WHERE state = 'held'");
      pass(s, clock);
      expect(alerts[alerts.length - 1].problem).toBeNull();
      expect(a).toBeTruthy();
    }, pausedReserve(() => false));
  });

  for (const refusal of ['unknown_workspace', 'repo_directory_unavailable', 'parent_not_found', 'invalid_launch_request']) {
    it(`cancels a hold whose re-placement is refused with ${refusal}`, async () => {
      let gone = false;
      await fixture(async (s, sql, clock) => {
        const id = await createHeld(s, sql, 'provider_disabled', 3600_000, clock);
        gone = true;
        expect(pass(s, clock).cancelled).toBe(1);
        expect(row(sql, id).state).toBe('cancelled');
        expect(JSON.parse(row(sql, id).hold_json).reason).toBe(refusal);
      }, input => gone
        ? { status: 400, value: { error: refusal } }
        : { status: 409, value: { error: 'no_eligible_route', rejected: [{ machine: 'hetzner', engine: 'claude', reason: 'provider_disabled' }] } });
    });
  }

  const link = (sql: any, id: string, patch: Record<string, string>) => {
    for (const [k, v] of Object.entries(patch)) sql.exec(`UPDATE session_control_sessions SET request_json = json_set(request_json, '$.${k}', ?) WHERE id = ?`, v, id);
  };
  const holdReserve = (calls: any[]) => (input: any) => {
    calls.push(input);
    return { status: 409, value: { error: 'no_eligible_route', rejected: [{ machine: 'hetzner', engine: 'claude', reason: 'provider_disabled' }] } };
  };

  it('cancels holds on a worktree another intake owns or that was removed, and pins re-placement to the intake host', async () => {
    const calls: any[] = [];
    await fixture(async (s, sql, clock) => {
      const path = (n: string) => `/home/u/git/app-wt-${n}`;
      const wt = (task: string, p: string, state: string) => sql.exec(
        "INSERT INTO intake_worktrees (id, task_id, machine, path, created_at, updated_at, session_ids, origin, state) VALUES (?, ?, 'hetzner', ?, 1, 1, '[]', 'session_cwd', ?)", crypto.randomUUID(), task, p, state);
      const owned = await createHeld(s, sql, 'provider_disabled', 3600_000, clock);
      const removed = await createHeld(s, sql, 'provider_disabled', 3500_000, clock);
      const pinned = await createHeld(s, sql, 'provider_disabled', 3400_000, clock);
      wt('someone-else', path('owned'), 'active'); link(sql, owned, { intake_task_id: 'fixture-intake', workspace: path('owned') });
      wt('fixture-intake', path('gone'), 'removed'); link(sql, removed, { intake_task_id: 'fixture-intake', workspace: path('gone') });
      wt('fixture-intake', path('mine'), 'active'); link(sql, pinned, { intake_task_id: 'fixture-intake' });
      calls.length = 0;
      pass(s, clock);
      expect(JSON.parse(row(sql, owned).hold_json).reason).toBe('worktree_owned_by_other_intake');
      expect(JSON.parse(row(sql, removed).hold_json).reason).toBe('intake_worktree_removed');
      expect(row(sql, owned).state).toBe('cancelled');
      expect(row(sql, removed).state).toBe('cancelled');
      expect(row(sql, pinned).state).toBe('held');
      expect(calls.length).toBe(1);
      expect(calls[0].machine).toBe('hetzner');
    }, holdReserve(calls));
  });

  it('re-places with the stored needs constraint', async () => {
    const calls: any[] = [];
    await fixture(async (s, sql, clock) => {
      const id = await createHeld(s, sql, 'provider_disabled', 3600_000, clock);
      sql.exec("UPDATE session_control_sessions SET request_json = json_set(request_json, '$.needs', json('[\"gpu\"]')) WHERE id = ?", id);
      calls.length = 0;
      pass(s, clock);
      expect(calls[0].needs).toEqual(['gpu']);
    }, holdReserve(calls));
  });

  it('does not rewrite hold_json or re-reserve non-transient rows on telemetry drift or inside the slow cadence', async () => {
    let tick = 0;
    const calls: any[] = [];
    await fixture(async (s, sql, clock) => {
      const id = await createHeld(s, sql, 'provider_disabled', 3600_000, clock);
      pass(s, clock);
      const before = sql.exec('SELECT hold_json, updated_at FROM session_control_sessions WHERE id = ?', id).toArray()[0] as any;
      pass(s, clock); pass(s, clock);
      const after = sql.exec('SELECT hold_json, updated_at FROM session_control_sessions WHERE id = ?', id).toArray()[0] as any;
      expect(after).toEqual(before);
      // Inside the slow interval: no reserve at all.
      const n = calls.length;
      clock.t += 10_000; s.reconcileHeldSessions(20);
      clock.t += 10_000; s.reconcileHeldSessions(20);
      expect(calls.length).toBe(n);
      expect(HELD_SLOW_INTERVAL_MS).toBeGreaterThan(10_000);
    }, input => {
      calls.push(input);
      return { status: 409, value: { error: 'no_eligible_route', rejected: [{ machine: 'hetzner', engine: 'claude', reason: 'provider_disabled', memory: ++tick, advertised_at: String(tick) }] } };
    });
  });

  it('recomputes the stale alert on a slow cadence and never rewrites it as time passes', async () => {
    await fixture(async (s, sql, clock, alerts) => {
      await createHeld(s, sql, 'provider_disabled', HELD_ALERT_MS + 3600_000, clock);
      pass(s, clock);
      expect(alerts.filter(x => x.problem).length).toBe(1);
      for (let i = 0; i < 30; i++) { clock.t += 3600_000 / 6; s.reconcileHeldSessions(20); } // 5 hours of drift
      expect(alerts.length).toBe(1);
      expect(HELD_ALERT_INTERVAL_MS).toBeGreaterThan(10_000);
    }, pausedReserve(() => false));
  });
});
