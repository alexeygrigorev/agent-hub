/**
 * Rows-read bounds for the two host-polled fleet routes (intake e8a22f4c):
 * GET /api/fleet/offers and PUT /api/fleet/sessions. Seeds a production-shaped
 * history (thousands of retained launches, managed sessions and census rows)
 * and asserts each call reads rows proportional to what changed, not to history.
 */
import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const now = Date.UTC(2026, 9, 31, 12, 0, 0);
const iso = (offset = 0) => new Date(now + offset).toISOString();
const HOSTS = 8, HISTORY = 400, CENSUS = 500;
const host = { caller: { machineId: 'h0', kind: 'host', actor: 'machine:h0' }, scopes: ['host'] } as any;
const uuid = (p: number, i: number) => `${String(p).padStart(8, '0')}-0000-4000-8000-${String(i).padStart(12, '0')}`;

/** Wrap sql so every cursor is kept; rowsRead is final once consumed. */
function tracked(sql: any) {
  const calls: { q: string; cursor: any }[] = [];
  const proxy = new Proxy(sql, { get(t, p) {
    if (p === 'exec') return (q: string, ...b: any[]) => { const cursor = t.exec(q, ...b); calls.push({ q, cursor }); return cursor; };
    const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
  } });
  /** Rows read by statements issued since `from` (a prior `mark()`), total and per statement. */
  const report = (from = 0) => {
    const by = new Map<string, number>(); let total = 0;
    for (const { q, cursor } of calls.slice(from)) { const n = Number(cursor.rowsRead) || 0; total += n; const k = q.replace(/\s+/g, ' ').slice(0, 110); by.set(k, (by.get(k) ?? 0) + n); }
    return { total, top: [...by].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k, n]) => `${n} ${k}`) };
  };
  const mark = () => calls.length;
  return { proxy, report, mark };
}

function seed(sql: any) {
  for (let m = 0; m < HOSTS; m++) sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?,?,?,?,?,0)", 'h' + m, 'h' + m, 'h' + m, iso(), iso());
  for (let m = 0; m < HOSTS; m++) for (let i = 0; i < HISTORY; i++) {
    const n = m * HISTORY + i, launch = uuid(1, n), native = uuid(3, n), live = i < 5;
    sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, launch, 'owner', 'agent', 's', 'k' + n, '{}', 'h' + m, 'codex', 'a', 'interactive', '/w', 'p', live ? 'running' : 'exited', native, iso(-86400000 + n), iso(-3600000 + n));
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, uuid(2, n), 'owner', 'r' + n, JSON.stringify({ prompt: 'x'.repeat(2000) }), launch, 'h' + m, 'codex', 'interactive', live ? 'running' : 'stopped',
      JSON.stringify({ native_id: native, tag: 'hub-' + launch, workspace: '/w', native_created_at_ms: now - 1000 - n }), iso(-86400000 + n), iso(-3600000 + n));
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,cwd,created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)`,
      'h' + m, native, 'hub-' + launch, 'codex', live ? 'running' : 'exited', null, iso(-60000), '/w', '/w', now - 1000 - n);
  }
  sql.exec("INSERT INTO fleet_catalog_seen VALUES ('h0', ?) ON CONFLICT(machine_id) DO UPDATE SET observed_at=excluded.observed_at", iso(-1000));
}

const withFleet = (fn: (s: FleetService, t: ReturnType<typeof tracked>, hub: Hub) => Promise<void>) => async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-rows-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    seed(hub.sql);
    const t = tracked(hub.sql);
    const s = new FleetService(t.proxy, (cb) => state.storage.transactionSync(cb), () => now);
    s.init();
    await fn(s, t, hub);
  });
};

it('PUT /api/fleet/sessions with an unchanged census reads rows per reported row, not per history', withFleet(async (s, t) => {
  const sessions = Array.from({ length: CENSUS }, (_, i) => ({
    native_id: uuid(3, i), tag: 'hub-' + uuid(1, i), engine: 'codex', phase: i < 5 ? 'running' : 'exited', confirmed_exit: i >= 5, agent_state: null,
    workspace: '/w', cwd: '/w', profile: null, account: null, last_activity_at: null, created_at_ms: now - 1000 - i }));
  const put = () => s.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions }) }), host);
  expect((await put()).status).toBe(200); // first census: rows differ in observed state, so it may write
  const from = t.mark();
  expect((await put()).status).toBe(200);
  const after = t.report(from);
  const second = after.total;
  expect(second, `unchanged PUT top statements: ${JSON.stringify(after.top)}`).toBeLessThanOrEqual(CENSUS * 4);
}));

it('GET /api/fleet/offers with no offers reads a bounded number of rows', withFleet(async (s, t) => {
  const get = () => s.handle(new Request('https://hub.test/api/fleet/offers'), host);
  expect((await get()).status).toBe(200);
  const from = t.mark();
  expect((await get()).status).toBe(200);
  const after = t.report(from);
  const second = after.total;
  expect(second, `idle offers top statements: ${JSON.stringify(after.top)}`).toBeLessThanOrEqual(400);
}));

it('PUT /api/fleet/sessions still writes changed rows, retires vanished live rows and skips stopped sessions', withFleet(async (s, _t, hub) => {
  const row = (i: number, phase: string, extra: any = {}) => ({
    native_id: uuid(3, i), tag: 'hub-' + uuid(1, i), engine: 'codex', phase, confirmed_exit: phase === 'exited', agent_state: null,
    workspace: '/w', cwd: '/w', profile: null, account: null, last_activity_at: null, created_at_ms: now - 1000 - i, ...extra });
  const put = async (sessions: any[]) => { const r = await s.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions }) }), host); expect(r.status).toBe(200); return r.json() as Promise<any>; };
  const phaseOf = (i: number) => (hub.sql.exec('SELECT phase FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', 'h0', uuid(3, i)).toArray()[0] as any)?.phase;
  // Seeded rows 0-4 are live (running) and the rest are exited history; the census reports 0-2 only.
  expect((await put([row(0, 'running'), row(1, 'running'), row(2, 'running')])).written).toBeGreaterThan(0);
  expect(phaseOf(2)).toBe('running');
  // Unchanged census: nothing written.
  expect((await put([row(0, 'running'), row(1, 'running'), row(2, 'running')])).written).toBe(0);
  // Row 1 vanished from the catalog (live -> unobserved); row 2 changed phase.
  expect((await put([row(0, 'running'), row(2, 'exited')])).written).toBe(2);
  expect(phaseOf(1)).toBe('unobserved');
  expect(phaseOf(2)).toBe('exited');
  // Unreported live rows (3, 4) were retired by the first census; unreported exited history is untouched.
  expect(phaseOf(3)).toBe('unobserved');
  expect(phaseOf(10)).toBe('exited');
}));
